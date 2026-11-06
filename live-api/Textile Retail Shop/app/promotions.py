"""The promotion engine — what a cart has earned, and what that costs in stock.

Nothing in this file knows about chudithars or leggings. It reads the scheme
rows in app/models.py and answers one question for a cart:

    which schemes does this cart satisfy, how many times, and what does the
    customer get for it

The billing code calls it twice and never decides anything itself. The counter
calls `evaluate` on every cart change so the cashier can see the free item
appear; `checkout` calls the SAME function again, from the products it is about
to bill, and adds the reward lines from that answer. The client is never
believed about free goods — it sends what the customer is paying for and gets
back what the shop is giving away, which is the only arrangement where a
modified page cannot bill itself a free garment.

THREE THINGS THIS REFUSES TO GUESS
----------------------------------

**A free item is stock.** A reward line reduces the product's quantity and
writes a stock movement exactly as a sold one does, under its own reason so the
ledger says which it was. A promotion that "costs nothing" is a promotion that
walks garments out of the door with no record — see `apply_to_invoice`.

**It checks the same stock figure the till does.** `Product.stock_qty`, the shop
total, and not the branch split in `LocationStock`. The till already refuses a
sale on that figure and deliberately does not consult the split (a shop whose
branches were stocked before that table existed has a total and no split), so
holding free goods to a stricter test than sold ones would mean promotions
silently never firing at exactly those branches. One rule, both kinds of line.

**It never spends the same garment twice.** Schemes run in priority order and
each takes its qualifying items out of the pool, so the next scheme sees only
what is left. `stackable` is the deliberate exception, and it is a per-scheme
decision an admin has to make rather than something that falls out of the order
two offers happened to be created in.

TAX ON A FREE ITEM. The reward line is billed at zero, so its taxable value and
its GST are zero, and the tax on the bill is the tax on what was actually
charged. This is the ordinary treatment of goods given away with a purchase —
the consideration, and the tax on it, sits in the qualifying items' price. A
scheme that instead needs the reward taxed at a notional value would be a
different scheme type, not a different sum here.
"""
from datetime import date

from sqlalchemy import or_

from app import db
from app.models import (InvoiceItem, Product, PromotionApplication,
                        PromotionAudit, PromotionScheme)

#: Quantities are floats — the shop sells fabric by the metre — so "is this a
#: whole set" is a comparison with a tolerance, not an equality. A paisa's worth
#: of float error must not cost a customer their free garment.
QTY_EPS = 1e-6

#: What a reward line's stock movement is filed under. Its own reason rather than
#: `sale`, so the ledger can be asked what left the building as a promotion
#: without inferring it from a price of zero — which is also what a mis-keyed
#: line looks like.
MOVEMENT_REASON = "promo"


# --------------------------------------------------------------------------
# where a till is
# --------------------------------------------------------------------------

class Place:
    """The four things that say where a bill is being raised.

    A plain carrier, not a query: the till already knows all four (see
    app/places.py) and passing them in keeps this module free of Flask session
    state, which is what lets a test evaluate a cart with no request at all.
    """

    def __init__(self, warehouse_id=None, company=None, location=None, counter=None):
        self.warehouse_id = warehouse_id
        self.company_id = getattr(company, "id", company)
        self.location_id = getattr(location, "id", location)
        self.counter_id = getattr(counter, "id", counter)

    def matches(self, row):
        """Does one PromotionPlace row cover this till?

        A row matches when every column it FILLS IN matches. A row naming only a
        location is "this branch, any counter"; one naming a counter as well is
        that till alone. A column left blank is not a wildcard by accident — it
        is the admin saying they do not care about that level.
        """
        for mine, theirs in ((self.warehouse_id, row.warehouse_id),
                             (self.company_id, row.company_id),
                             (self.location_id, row.location_id),
                             (self.counter_id, row.counter_id)):
            if theirs is not None and theirs != mine:
                return False
        return True


ANYWHERE = Place()


# --------------------------------------------------------------------------
# reading the scheme master
# --------------------------------------------------------------------------

def active_schemes(when=None, place=None):
    """Every scheme that could apply here today, in the order they get a look.

    Priority ascending, then id: a numbered list of offers reads 1, 2, 3, and
    two schemes given the same number are settled by which was created first
    rather than by whatever the database felt like returning.
    """
    when = when or date.today()
    place = place or ANYWHERE
    rows = (PromotionScheme.query
            .filter(PromotionScheme.active.is_(True))
            .order_by(PromotionScheme.priority, PromotionScheme.id).all())
    out = []
    for s in rows:
        if not s.runs_on(when):
            continue
        # No place rows at all means everywhere. Otherwise ANY row that covers
        # this till lets the scheme in — the rows are alternatives ("Tirupur or
        # Karur"), which is the only reading that lets one scheme name several
        # branches without a row per combination.
        if s.places and not any(place.matches(p) for p in s.places):
            continue
        if not s.conditions or not s.rewards:
            continue          # a half-built scheme gives nothing away
        out.append(s)
    return out


def describe(scheme):
    """One line a person can read: 'Buy 3 LADIES CHUDITHAR → 1 LEGGINGS free'.

    Written from the rows rather than stored, so it cannot fall out of step with
    what the scheme actually does — which is the whole failure mode of a
    description somebody types in.
    """
    def side(rows):
        names = [r.label for r in rows]
        if not names:
            return "anything"
        if len(names) <= 2:
            return " or ".join(names)
        return f"{names[0]} or {len(names) - 1} other(s)"

    buys = " + ".join(
        f"{_trim(c.min_qty)} × {c.label or side(c.items)}" for c in scheme.conditions)
    gets = " + ".join(
        f"{_trim(r.qty)} × {side(r.items)}" for r in scheme.rewards)
    tail = {"buy_get_free": "free",
            "buy_get_percent": f"at {_trim(_first_value(scheme))}% off",
            "buy_get_price": f"at ₹{_trim(_first_value(scheme))}"}.get(
        scheme.scheme_type, "free")
    return f"Buy {buys} → {gets} {tail}"


def _first_value(scheme):
    return scheme.rewards[0].value if scheme.rewards else 0


def _trim(n):
    """3.0 → '3', 2.5 → '2.5'. Quantities are floats and mostly whole."""
    n = float(n or 0)
    return str(int(n)) if abs(n - int(n)) < QTY_EPS else f"{n:g}"


# --------------------------------------------------------------------------
# matching products to a scheme's rows
# --------------------------------------------------------------------------

def _row_matches(rows, product):
    """Is `product` named by any of these condition / reward item rows?

    By id, both ways. A product row is that exact SKU; a category row is every
    product filed under it, which is what makes "any 3 from LADIES CHUDITHAR"
    follow the category master instead of a list somebody has to maintain.
    """
    for r in rows:
        if r.product_id and r.product_id == product.id:
            return True
        if r.category_id and r.category_id == product.category_id:
            return True
    return False


def eligible_reward_products(reward, exclude_ids=()):
    """Every product this reward could be satisfied by, in stock order.

    Category rows are expanded through the product master rather than copied, so
    a garment added to LEGGINGS tomorrow is rewardable tomorrow with nobody
    editing the scheme.
    """
    ids = [r.product_id for r in reward.items if r.product_id]
    cats = [r.category_id for r in reward.items if r.category_id]
    if not ids and not cats:
        return []
    clauses = []
    if ids:
        clauses.append(Product.id.in_(ids))
    if cats:
        clauses.append(Product.category_id.in_(cats))
    q = Product.query.filter(Product.active.is_(True), or_(*clauses))
    if exclude_ids:
        q = q.filter(~Product.id.in_(list(exclude_ids)))
    return q.order_by(Product.selling_price, Product.id).all()


# --------------------------------------------------------------------------
# what a cart earned
# --------------------------------------------------------------------------

class RewardLine:
    """One reward product, at the quantity and price the promotion sets it to."""

    def __init__(self, product, qty, unit_price, value, substituted=False):
        self.product = product
        self.qty = qty
        self.unit_price = unit_price
        #: what the customer was given, priced at what it would have sold for
        self.value = value
        self.substituted = substituted

    def to_json(self):
        return {"product_id": self.product.id, "sku": self.product.sku,
                "name": self.product.name, "qty": self.qty,
                "unit_price": round(self.unit_price, 2),
                # The counter adds a discounted reward to its own running total,
                # and a total without the tax on it would disagree with the bill
                # the server strikes — which is the one figure at a till that has
                # to balance to the paisa.
                "gst": float(self.product.gst_rate or 0),
                "value": round(self.value, 2),
                "free": not self.unit_price,
                "substituted": self.substituted}


class Award:
    """One scheme, earned by this cart, this many times."""

    def __init__(self, scheme, times, consumed, rewards, options=(), note=""):
        self.scheme = scheme
        self.times = times
        #: {product_id: qty} — which garments were spent earning it
        self.consumed = consumed
        self.rewards = list(rewards)
        #: other products the cashier could take instead, when the scheme allows
        self.options = list(options)
        self.note = note

    @property
    def qualifying_qty(self):
        return round(sum(self.consumed.values()), 3)

    @property
    def reward_qty(self):
        return round(sum(r.qty for r in self.rewards), 3)

    @property
    def benefit(self):
        return round(sum(r.value for r in self.rewards), 2)

    def to_json(self):
        return {"scheme_id": self.scheme.id, "code": self.scheme.code,
                "name": self.scheme.name, "description": describe(self.scheme),
                "times": self.times, "qualifying_qty": self.qualifying_qty,
                "reward_qty": self.reward_qty, "benefit": self.benefit,
                "rewards": [r.to_json() for r in self.rewards],
                "options": [{"product_id": p.id, "sku": p.sku, "name": p.name,
                             "price": p.selling_price, "stock": p.stock_qty}
                            for p in self.options],
                "note": self.note}


class Notice:
    """A scheme that qualified and gave nothing, and why.

    Kept apart from the awards because it is the thing the counter has to SAY:
    a cart that earned a free garment the shop hasn't got must not silently look
    like a cart that earned nothing.
    """

    def __init__(self, scheme, kind, message):
        self.scheme = scheme
        self.kind = kind
        self.message = message

    def to_json(self):
        return {"scheme_id": self.scheme.id, "code": self.scheme.code,
                "name": self.scheme.name, "kind": self.kind,
                "message": self.message}


class Outcome:
    def __init__(self, awards, notices):
        self.awards = awards
        self.notices = notices

    def to_json(self):
        return {"awards": [a.to_json() for a in self.awards],
                "notices": [n.to_json() for n in self.notices],
                "free_qty": round(sum(a.reward_qty for a in self.awards), 3),
                "benefit": round(sum(a.benefit for a in self.awards), 2)}


def evaluate(cart, place=None, when=None, choices=None):
    """What `cart` has earned. The one entry point both billing paths use.

    `cart` is [{product_id, quantity}, …] — what the customer is PAYING for.
    Reward lines are never passed in and never trusted from a caller; they are
    what this returns.

    `choices` is {scheme_id: product_id} for a scheme that lets the cashier pick
    the reward. A choice that is not an eligible reward is ignored rather than
    honoured — that field arrives from a browser.
    """
    place = place or ANYWHERE
    choices = {int(k): int(v) for k, v in (choices or {}).items() if v}

    # The cart, resolved once. A dict keyed by product id because a till can put
    # the same product on two lines and the promotion cares about the total.
    pool, products = {}, {}
    for row in cart:
        pid = int(row.get("product_id") or row.get("id") or 0)
        qty = float(row.get("quantity") or row.get("qty") or 0)
        if not pid or qty <= 0:
            continue
        p = products.get(pid) or db.session.get(Product, pid)
        if p is None:
            continue
        products[pid] = p
        pool[pid] = round(pool.get(pid, 0.0) + qty, 3)
    if not pool:
        return Outcome([], [])

    # Stock already spoken for by this same cart, so a scheme that rewards a
    # garment the customer is also buying cannot hand out the last one twice.
    committed = dict(pool)
    #: The cart as it arrived, never consumed. A stackable scheme reads THIS,
    #: which is what stacking means: it gets the whole purchase to qualify
    #: against, regardless of what an earlier scheme has already claimed.
    whole_cart = dict(pool)

    awards, notices = [], []
    for scheme in active_schemes(when=when, place=place):
        working = dict(whole_cart) if scheme.stackable else dict(pool)
        sets, consumed, leftover = _count_sets(scheme, working, products)
        if sets <= 0:
            continue

        rewards, granted, note = _grant(scheme, sets, committed, choices)
        if granted <= 0:
            notices.append(Notice(
                scheme, "blocked_no_stock",
                f"{scheme.name}: promotion eligible, but the free item is "
                f"currently out of stock."))
            continue
        if granted < sets:
            # Part of what was earned could be given. Said out loud rather than
            # quietly handing over fewer — the customer earned the other one,
            # and only the sets actually rewarded may spend qualifying goods.
            short = sets - granted
            notices.append(Notice(
                scheme, "blocked_no_stock",
                f"{scheme.name}: {short} more free item set(s) earned, but the "
                f"reward is out of stock."))
            sets, consumed, leftover = _count_sets(scheme, working, products,
                                                   limit=granted)

        if not scheme.stackable:
            pool = leftover

        awards.append(Award(scheme, granted, consumed, rewards,
                            options=_reward_options(scheme, rewards, committed),
                            note=note))

    return Outcome(awards, notices)


def _count_sets(scheme, pool, products, limit=None):
    """How many complete sets `pool` builds, and what that spends.

    Built one set at a time rather than by dividing, because a scheme with two
    conditions ("2 chudithars AND 1 dupatta") has to satisfy both out of the
    SAME cart, and a product can match both conditions. Taking a set at a time
    out of a scratch copy is exact for that; dividing each condition separately
    would count a dual-matching garment twice.

    Within a condition the most SPECIFIC garment is spent first — the one that
    matches fewest of this scheme's other conditions. Spending a dual-matching
    piece on the first condition when a single-matching one would have done is
    how a cart that plainly qualifies comes out one short.
    """
    cap = scheme.max_applications if limit is None else limit
    if scheme.max_applications is not None and limit is not None:
        cap = min(limit, scheme.max_applications)

    conditions = list(scheme.conditions)
    breadth = {pid: sum(1 for c in conditions
                        if _row_matches(c.items, products[pid])) for pid in pool}

    sets, consumed = 0, {}
    while cap is None or sets < cap:
        trial = dict(pool)
        taken = {}
        complete = True
        for cond in conditions:
            need = float(cond.min_qty or 0)
            if need <= 0:
                continue
            candidates = sorted(
                (pid for pid in trial
                 if trial[pid] > QTY_EPS and _row_matches(cond.items, products[pid])),
                key=lambda pid: (breadth.get(pid, 99), pid))
            for pid in candidates:
                if need <= QTY_EPS:
                    break
                use = min(need, trial[pid])
                trial[pid] = round(trial[pid] - use, 3)
                taken[pid] = round(taken.get(pid, 0.0) + use, 3)
                need = round(need - use, 6)
            if need > QTY_EPS:
                complete = False
                break
        if not complete:
            break
        pool = trial
        sets += 1
        for pid, qty in taken.items():
            consumed[pid] = round(consumed.get(pid, 0.0) + qty, 3)

    return sets, consumed, pool


def _grant(scheme, sets, committed, choices):
    """Turn `sets` earned into reward lines, as far as stock allows.

    Returns (lines, sets_granted, note). `committed` is updated in place with
    whatever the rewards take, so two schemes on one bill cannot both promise
    the last leggings.

    Counted DOWN from what was earned rather than divided out of stock, because
    a scheme can hand over more than one thing per set and they have to move
    together — three of one reward and two of the other is not two-and-a-bit
    sets, it is a bill nobody can explain. So the whole basket is tried at
    `sets`, then at `sets - 1`, until one fits. At a till `sets` is a handful.
    """
    cache = {}
    granted, picked = sets, []
    while granted > 0:
        trial, picked, fits = dict(committed), [], True
        for reward in scheme.rewards:
            per_set = float(reward.qty or 0)
            if per_set <= 0:
                continue
            qty = round(per_set * granted, 3)
            product, alt = _pick_reward(scheme, reward, qty, trial, choices, cache)
            if product is None:
                fits = False
                break
            picked.append((reward, product, qty, alt))
            trial[product.id] = round(trial.get(product.id, 0.0) + qty, 3)
        if fits and picked:
            break
        granted -= 1

    if granted <= 0 or not picked:
        return [], 0, ""

    lines, note = [], ""
    for reward, product, qty, alt in picked:
        unit_price = _reward_price(scheme, reward, product)
        full = float(product.selling_price or 0)
        lines.append(RewardLine(product, qty, unit_price,
                                round(qty * max(0.0, full - unit_price), 2),
                                substituted=alt))
        committed[product.id] = round(committed.get(product.id, 0.0) + qty, 3)
        if alt:
            note = (f"{product.name} given instead — the scheme's usual reward "
                    f"is out of stock.")

    return lines, granted, note


def _pick_reward(scheme, reward, qty_needed, committed, choices, cache=None):
    """Which product this reward hands over, and whether it is a substitute.

    Returns (product, substituted). None when the scheme's rule cannot be
    honoured at all — the caller turns that into the out-of-stock notice.
    """
    cache = {} if cache is None else cache
    if reward.id not in cache:
        cache[reward.id] = eligible_reward_products(reward)
    candidates = cache[reward.id]
    if not candidates:
        return None, False

    def free(p):
        return (p.stock_qty or 0) - committed.get(p.id, 0)

    # What the scheme SAYS to give, before stock is considered.
    wanted = None
    chosen_id = choices.get(scheme.id)
    if chosen_id and scheme.reward_selection in ("choose", "cheapest", "dearest"):
        wanted = next((p for p in candidates if p.id == chosen_id), None)
    if wanted is None:
        if scheme.reward_selection == "dearest":
            wanted = max(candidates, key=lambda p: (p.selling_price or 0, p.id))
        elif scheme.reward_selection == "specific":
            wanted = candidates[0]
        else:                       # cheapest, and the default for `choose`
            wanted = min(candidates, key=lambda p: (p.selling_price or 0, p.id))

    if free(wanted) + QTY_EPS >= qty_needed:
        return wanted, False
    if scheme.out_of_stock != "substitute":
        return None, False

    # Substitution: the next eligible product that IS on the shelf, cheapest
    # first — a promotion is a cost, and giving away the dearest thing in the
    # category because it happened to be in stock is not what was agreed.
    for p in sorted(candidates, key=lambda p: (p.selling_price or 0, p.id)):
        if p.id != wanted.id and free(p) + QTY_EPS >= qty_needed:
            return p, True
    return None, False


def _reward_price(scheme, reward, product):
    """What the reward line is billed at."""
    full = float(product.selling_price or 0)
    if scheme.scheme_type == "buy_get_percent":
        return round(full * (1 - min(100.0, max(0.0, reward.value or 0)) / 100.0), 2)
    if scheme.scheme_type == "buy_get_price":
        return round(min(full, max(0.0, reward.value or 0)), 2)
    return 0.0                      # buy_get_free


def _reward_options(scheme, rewards, committed):
    """Other products the cashier may swap the reward for, if the scheme allows.

    Only for a scheme configured to let somebody choose. Offering alternatives
    on a scheme whose reward is fixed would invite a cashier to give away
    something the offer never promised.
    """
    if scheme.reward_selection != "choose" or not scheme.rewards:
        return []
    given = {r.product.id for r in rewards}
    need = float(scheme.rewards[0].qty or 1)
    return [p for p in eligible_reward_products(scheme.rewards[0])
            if p.id not in given
            and (p.stock_qty or 0) - committed.get(p.id, 0) + QTY_EPS >= need][:12]


# --------------------------------------------------------------------------
# writing it onto a bill
# --------------------------------------------------------------------------

def apply_to_invoice(invoice, outcome, sold_items, place=None, user_id=None):
    """Record the awards against `invoice` and hand back the reward lines.

    Called from inside the checkout transaction, after the paid lines exist and
    before the totals are struck. Returns [(InvoiceItem, Product, qty)] for the
    reward lines so the caller moves their stock the same way it moves its own —
    this function deliberately does NOT touch stock itself. Billing owns the
    ledger; a promotion that wrote movements from over here would be a second
    place stock can leave the building from.

    `sold_items` is {product_id: [InvoiceItem, …]} for the lines already added,
    so the qualifying ones can be marked with what the scheme spent. A list
    rather than a single line because a till may put the same garment on the
    bill twice at different prices, and the promotion spent some of each.
    """
    place = place or ANYWHERE
    created = []

    for award in outcome.awards:
        app_row = PromotionApplication(
            invoice_id=invoice.id,
            scheme_id=award.scheme.id,
            scheme_code=award.scheme.code,
            scheme_name=award.scheme.name,
            times_applied=award.times,
            qualifying_qty=award.qualifying_qty,
            reward_qty=award.reward_qty,
            benefit_value=award.benefit,
            status="applied",
        )
        db.session.add(app_row)
        db.session.flush()

        # Mark what earned it. `promo_qty` is what the scheme SPENT, which is
        # not the whole line: five chudithars earning one set of three qualified
        # three of them, and a report that counted five would credit the scheme
        # with goods it never moved.
        # A line records the FIRST scheme that spent it. Only a stackable scheme
        # can want a line somebody else already claimed, and the second claim is
        # still counted in full on its own application row — what it cannot do
        # is rewrite the line to say it belonged to the later offer. One column,
        # one answer, and the aggregate stays right either way.
        for pid, qty in award.consumed.items():
            left = qty
            for item in sold_items.get(pid) or []:
                if left <= QTY_EPS:
                    break
                if item.promo_application_id not in (None, app_row.id):
                    continue
                spare = float(item.quantity) - float(item.promo_qty or 0)
                use = min(left, max(0.0, spare))
                if use <= QTY_EPS:
                    continue
                item.promo_application_id = app_row.id
                item.promo_role = "qualifying"
                item.promo_qty = round((item.promo_qty or 0) + use, 3)
                left = round(left - use, 6)

        for line in award.rewards:
            tax_rate = float(line.product.gst_rate or 0)
            line_total = round(line.qty * line.unit_price, 2)
            item = InvoiceItem(
                invoice_id=invoice.id,
                product_id=line.product.id,
                quantity=line.qty,
                unit_price=line.unit_price,
                gst_rate=tax_rate,
                line_total=line_total,
                tax_amount=round(line_total * tax_rate / 100.0, 2),
                promo_application_id=app_row.id,
                promo_role="reward",
                promo_qty=line.qty,
                promo_value=line.value,
            )
            db.session.add(item)
            created.append((item, line.product, line.qty))

        db.session.add(PromotionAudit(
            scheme_id=award.scheme.id, scheme_code=award.scheme.code,
            scheme_name=award.scheme.name, invoice_id=invoice.id,
            application_id=app_row.id,
            event="substituted" if any(r.substituted for r in award.rewards)
                  else "applied",
            detail=(describe(award.scheme)
                    + (f" — {award.note}" if award.note else "")),
            times_applied=award.times, qualifying_qty=award.qualifying_qty,
            reward_qty=award.reward_qty, benefit_value=award.benefit,
            company_id=place.company_id, location_id=place.location_id,
            counter_id=place.counter_id, user_id=user_id))

    # The refusals matter as much as the awards: "why did this customer not get
    # theirs" has no answer anywhere else.
    for notice in outcome.notices:
        db.session.add(PromotionAudit(
            scheme_id=notice.scheme.id, scheme_code=notice.scheme.code,
            scheme_name=notice.scheme.name, invoice_id=invoice.id,
            event=notice.kind, detail=notice.message,
            company_id=place.company_id, location_id=place.location_id,
            counter_id=place.counter_id, user_id=user_id))

    return created


# --------------------------------------------------------------------------
# what a return does to a promotion
# --------------------------------------------------------------------------

class ReturnFinding:
    """One promotion that the goods coming back no longer pay for."""

    def __init__(self, application, sets_before, sets_after, unearned_qty,
                 unearned_value, returning_reward_qty, policy):
        self.application = application
        self.sets_before = sets_before
        self.sets_after = sets_after
        #: reward units the customer no longer qualifies for
        self.unearned_qty = unearned_qty
        #: …and what they are worth, at what they would have sold for
        self.unearned_value = unearned_value
        #: how many of those the customer is handing back on this same return
        self.returning_reward_qty = returning_reward_qty
        self.policy = policy

    @property
    def kept_qty(self):
        """Unearned free goods the customer is walking away with."""
        return max(0.0, round(self.unearned_qty - self.returning_reward_qty, 3))

    @property
    def clawback(self):
        """What to take off the refund for those, under this scheme's policy."""
        if self.policy != "reclaim_value" or self.unearned_qty <= 0:
            return 0.0
        per_unit = (self.unearned_value / self.unearned_qty) if self.unearned_qty else 0
        return round(self.kept_qty * per_unit, 2)

    def to_json(self):
        return {"application_id": self.application.id,
                "scheme": self.application.scheme_name,
                "code": self.application.scheme_code,
                "sets_before": self.sets_before, "sets_after": self.sets_after,
                "unearned_qty": self.unearned_qty,
                "unearned_value": round(self.unearned_value, 2),
                "kept_qty": self.kept_qty, "clawback": self.clawback,
                "policy": self.policy, "message": self.message}

    @property
    def message(self):
        name = self.application.scheme_name or "promotion"
        if self.unearned_qty <= 0:
            return ""
        if self.kept_qty <= 0:
            return (f"{name}: the free item is coming back with the purchase "
                    f"that earned it.")
        if self.policy == "require_return":
            return (f"{name}: {_trim(self.kept_qty)} free item(s) must come back "
                    f"with this return — the purchase no longer earns them.")
        if self.policy == "keep":
            return (f"{name}: {_trim(self.kept_qty)} free item(s) are no longer "
                    f"earned; this scheme lets the customer keep them.")
        return (f"{name}: {_trim(self.kept_qty)} free item(s) are no longer "
                f"earned — ₹{self.clawback:.2f} comes off the refund.")


def review_return(invoice, taking):
    """What the goods in `taking` do to the promotions on `invoice`.

    `taking` is {invoice_item_id: qty} — what the customer is handing back on
    this credit note, on top of anything already credited.

    The re-evaluation is deliberately arithmetic rather than a second run of the
    engine. Re-evaluating the surviving cart would let the shop QUIETLY re-earn
    the promotion from different garments, which is a generous answer to a
    question nobody asked; what a return has to settle is whether the sets that
    were actually given are still paid for. So it counts the qualifying
    quantities that survive, divides by what one set cost, and compares.
    """
    findings = []
    for app_row in invoice.promotions:
        if app_row.status == "reversed":
            continue
        scheme = app_row.scheme

        qualified = 0.0
        surviving = 0.0
        for item in app_row.qualifying_lines:
            used = float(item.promo_qty or 0)
            qualified += used
            # Everything already credited, plus what is going back now. A line's
            # promotion credit is spent last: the pieces that come back are the
            # ones NOT holding the offer up, until there are none left.
            gone = float(item.returned_qty or 0) + float(taking.get(item.id, 0) or 0)
            surviving += max(0.0, used - max(0.0, gone - (item.quantity - used)))

        if qualified <= 0:
            continue
        per_set = qualified / app_row.times_applied if app_row.times_applied else qualified
        sets_after = int((surviving + QTY_EPS) // per_set) if per_set else 0
        sets_after = min(sets_after, app_row.times_applied)
        if sets_after >= app_row.times_applied:
            continue                    # still earned in full

        lost = app_row.times_applied - sets_after
        per_set_reward = (app_row.reward_qty / app_row.times_applied
                          if app_row.times_applied else 0)
        per_set_value = (app_row.benefit_value / app_row.times_applied
                         if app_row.times_applied else 0)

        coming_back = sum(float(taking.get(i.id, 0) or 0)
                          for i in app_row.reward_lines)

        findings.append(ReturnFinding(
            application=app_row,
            sets_before=app_row.times_applied, sets_after=sets_after,
            unearned_qty=round(lost * per_set_reward, 3),
            unearned_value=round(lost * per_set_value, 2),
            returning_reward_qty=round(coming_back, 3),
            policy=(scheme.return_policy if scheme else "reclaim_value")
                   or "reclaim_value"))
    return findings


def settle_return(note, findings, user_id=None):
    """Write the findings onto a credit note that is being raised.

    Reduces each application to what is still earned, logs the event, and
    returns the total clawback for the caller to take off the refund.
    """
    total = 0.0
    for f in findings:
        app_row = f.application
        app_row.status = "reversed" if f.sets_after <= 0 else "reduced"
        # The application is reduced to what survives, so the promotion reports
        # stop counting goods the customer no longer qualifies for.
        per_set_reward = (app_row.reward_qty / app_row.times_applied
                          if app_row.times_applied else 0)
        per_set_value = (app_row.benefit_value / app_row.times_applied
                         if app_row.times_applied else 0)
        per_set_qual = (app_row.qualifying_qty / app_row.times_applied
                        if app_row.times_applied else 0)
        app_row.times_applied = f.sets_after
        app_row.reward_qty = round(per_set_reward * f.sets_after, 3)
        app_row.benefit_value = round(per_set_value * f.sets_after, 2)
        app_row.qualifying_qty = round(per_set_qual * f.sets_after, 3)

        total += f.clawback
        db.session.add(PromotionAudit(
            scheme_id=app_row.scheme_id, scheme_code=app_row.scheme_code,
            scheme_name=app_row.scheme_name, invoice_id=app_row.invoice_id,
            application_id=app_row.id,
            event="reversed" if f.sets_after <= 0 else "reduced",
            detail=f"{note.number}: {f.message}",
            times_applied=f.sets_after, qualifying_qty=app_row.qualifying_qty,
            reward_qty=app_row.reward_qty, benefit_value=app_row.benefit_value,
            company_id=note.invoice.company_id if note.invoice else None,
            location_id=note.invoice.location_id if note.invoice else None,
            counter_id=note.invoice.counter_id if note.invoice else None,
            user_id=user_id))
    return round(total, 2)
