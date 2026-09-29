// Offer authoring — the screen the rulebook never had (D11 §6, G1).
//
// Until now `POST /api/offers` was gated at `manage`, which exactly one persona
// holds (IT Admin), so every rule in the database had been seeded by code rather
// than written by a person. The gate is `operate` now and this is the surface
// behind it: the promo/marketing user writes the offer, and somebody answerable
// for the brand or the chain countersigns it before it can price a bill.
//
// Three decisions worth stating, because they are what keeps this screen honest:
//
//   · **Pattern first, dials second.** Nobody thinks "trigger: spend, reward:
//     amt_off". They think "spend ₹5,000, get ₹1,000 off". The picker names the
//     eight shapes the engine can actually pay, and each one asks only for its
//     own dials — so a shape the engine cannot read is not reachable from here.
//   · **The phrase is the proof.** After a save, the sentence shown is the
//     server's own `one_liner`, generated off the stored rule. The live preview
//     while typing is a courtesy and is labelled as one; if the two ever differ,
//     the server's is the true one and it is the one on screen.
//   · **Live rules are read-only here.** A running rule is not edited — it is
//     stopped and a new one written — because yesterday's bill has to stay
//     explicable by the rule that priced it.

import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { CalendarClock, Check, CircleSlash, Send, Tag } from "lucide-react";

import { PageHeader } from "../components/PageHeader";
import { api, apiErrorMessage } from "../lib/api";
import { OfferReturnCard } from "./OfferReturnCard";
import { OfferSimulationCard } from "./OfferSimulationCard";
import { useList } from "../lib/hooks";
import "./Booking.css";
import "./OffersPrice.css";

interface StoreOpt {
  id: number;
  code: string;
  name: string;
}
interface BrandOpt {
  id: number;
  code: string;
  name: string;
}

interface Offer {
  id: number;
  name: string;
  brand: string;
  brand_code: string;
  funder: string;
  layer: string;
  trigger_type: string;
  trigger_config: Record<string, unknown>;
  reward_type: string;
  reward_config: Record<string, unknown>;
  item_scope: Record<string, unknown>;
  store_scope: { kind?: string; stores?: string[] };
  stores: string[];
  starts_on: string;
  ends_on: string | null;
  combinable: boolean;
  priority: number;
  is_fallback: boolean;
  status: string;
  approved_by_name: string;
  one_liner: string;
  updated_at?: string;
  awaiting_approval?: boolean;
}

/** The eight shapes the engine can pay, in the words a person uses for them. */
interface Pattern {
  key: string;
  label: string;
  hint: string;
  trigger: string;
  reward: string;
  /** Which dials this shape asks for. */
  asks: ("percent" | "amount" | "price" | "buyget" | "gift" | "qtySlabs" | "spendSlabs")[];
}

const PATTERNS: Pattern[] = [
  {
    key: "flat_pct",
    label: "Flat % off",
    hint: "Every eligible piece, one percentage.",
    trigger: "none",
    reward: "pct_off",
    asks: ["percent"],
  },
  {
    key: "flat_amt",
    label: "Flat ₹ off",
    hint: "A fixed amount off each eligible line.",
    trigger: "none",
    reward: "amt_off",
    asks: ["amount"],
  },
  {
    key: "fixed_price",
    label: "One price",
    hint: "Anything eligible sells at this figure.",
    trigger: "none",
    reward: "fixed_price",
    asks: ["price"],
  },
  {
    key: "buy_get",
    label: "Buy X get Y free",
    hint: "The cheapest qualifying pieces come off.",
    trigger: "group",
    reward: "item_free",
    asks: ["buyget"],
  },
  {
    key: "qty_pct",
    label: "Buy more, % off",
    hint: "A ladder on pieces: buy 2 → 10%, buy 3 → 15%.",
    trigger: "qty",
    reward: "pct_off",
    asks: ["qtySlabs"],
  },
  {
    key: "spend_pct",
    label: "Spend & save %",
    hint: "A ladder on the bill: spend ₹5,000 → 10%.",
    trigger: "spend",
    reward: "pct_off",
    asks: ["spendSlabs"],
  },
  {
    key: "spend_amt",
    label: "Spend & save ₹",
    hint: "A ladder on the bill, paid as an amount.",
    trigger: "spend",
    reward: "amt_off",
    asks: ["spendSlabs"],
  },
  {
    key: "gift",
    label: "Gift with purchase",
    hint: "A real barcode leaves stock, at nil or a token price.",
    trigger: "spend",
    reward: "gift",
    asks: ["spendSlabs", "gift"],
  },
];

interface Step {
  min: string;
  value: string;
}

const TODAY = new Date().toISOString().slice(0, 10);

function patternOf(offer: Offer): Pattern {
  return (
    PATTERNS.find((p) => p.trigger === offer.trigger_type && p.reward === offer.reward_type) ??
    PATTERNS[0]
  );
}

export function OfferAuthorPage() {
  const { id } = useParams();
  const nav = useNavigate();
  const isNew = !id || id === "new";
  const { data: stores } = useList<StoreOpt>("/masters/stores");
  const { data: brands } = useList<BrandOpt>("/masters/brands");

  const [offer, setOffer] = useState<Offer | null>(null);
  const [pattern, setPattern] = useState<Pattern>(PATTERNS[0]);
  const [name, setName] = useState("");
  const [layer, setLayer] = useState("brand");
  const [funder, setFunder] = useState("brand");
  const [brand, setBrand] = useState("");
  const [percent, setPercent] = useState("20");
  const [amount, setAmount] = useState("500");
  const [price, setPrice] = useState("999");
  const [buy, setBuy] = useState("2");
  const [get, setGet] = useState("1");
  const [giftBarcode, setGiftBarcode] = useState("");
  const [giftToken, setGiftToken] = useState("0");
  // Ticket 11: the offer's own tax settings (§16), used where GST after
  // discount is switched on. Defaults are the baselines, CA to confirm.
  const [allocation, setAllocation] = useState("by_mrp");
  const [reducesValue, setReducesValue] = useState(false);
  const [steps, setSteps] = useState<Step[]>([{ min: "5000", value: "10" }]);
  const [scopeAll, setScopeAll] = useState(true);
  const [picked, setPicked] = useState<string[]>([]);
  const [startsOn, setStartsOn] = useState(TODAY);
  const [endsOn, setEndsOn] = useState("");
  const [combinable, setCombinable] = useState(false);
  const [priority, setPriority] = useState("100");
  const [mrpMin, setMrpMin] = useState("");
  const [mrpMax, setMrpMax] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [note, setNote] = useState("");

  useEffect(() => {
    if (isNew) return;
    api
      .get(`/offers/${id}`)
      .then((r) => {
        const row: Offer = r.data;
        setOffer(row);
        const shape = patternOf(row);
        setPattern(shape);
        setName(row.name);
        setLayer(row.layer);
        setFunder(row.funder);
        setBrand(row.brand_code || "");
        setStartsOn(row.starts_on);
        setEndsOn(row.ends_on ?? "");
        setCombinable(row.combinable);
        setPriority(String(row.priority));
        setScopeAll((row.store_scope?.kind ?? "all") === "all");
        setPicked(row.stores ?? []);
        const reward = row.reward_config ?? {};
        if (reward.percent) setPercent(String(reward.percent).replace(/\.00$/, ""));
        if (reward.amount_paise) setAmount(String(Number(reward.amount_paise) / 100));
        if (reward.price_paise) setPrice(String(Number(reward.price_paise) / 100));
        if (reward.gift_barcode) setGiftBarcode(String(reward.gift_barcode));
        if (reward.token_price_paise) setGiftToken(String(Number(reward.token_price_paise) / 100));
        setAllocation(reward.allocation === "free_piece" ? "free_piece" : "by_mrp");
        setReducesValue(reward.reduces_value === true);
        const trigger = row.trigger_config ?? {};
        if (trigger.buy) setBuy(String(trigger.buy));
        if (trigger.get) setGet(String(trigger.get));
        const slabs = (trigger.slabs as Record<string, unknown>[]) ?? [];
        if (slabs.length) {
          setSteps(
            slabs.map((slab) => ({
              min: String(
                slab.min_paise !== undefined
                  ? Number(slab.min_paise) / 100
                  : (slab.min_qty ?? ""),
              ),
              value: String(
                slab.percent !== undefined
                  ? String(slab.percent).replace(/\.00$/, "")
                  : slab.amount_paise !== undefined
                    ? Number(slab.amount_paise) / 100
                    : "",
              ),
            })),
          );
        }
        const scope = row.item_scope ?? {};
        if (scope.mrp_min_paise) setMrpMin(String(Number(scope.mrp_min_paise) / 100));
        if (scope.mrp_max_paise) setMrpMax(String(Number(scope.mrp_max_paise) / 100));
      })
      .catch((e) => setError(apiErrorMessage(e)));
  }, [id, isNew]);

  const frozen = offer !== null && (offer.status === "live" || offer.status === "ended");

  /** How it will read — a courtesy while typing. The server's phrase is the one
   *  shown once it is saved, and it is the authority. */
  const preview = useMemo(() => {
    const where = brands.find((b) => b.code === brand)?.name || (brand ? brand : "storewide");
    const top = steps[steps.length - 1];
    let what = "";
    if (pattern.reward === "pct_off") what = `${top && pattern.asks.includes("qtySlabs") || pattern.asks.includes("spendSlabs") ? top?.value || percent : percent}% off`;
    else if (pattern.reward === "amt_off")
      what = `₹${pattern.asks.includes("spendSlabs") ? top?.value || amount : amount} off`;
    else if (pattern.reward === "fixed_price") what = `flat ₹${price}`;
    else if (pattern.reward === "item_free") what = `buy ${buy} get ${get} free`;
    else if (pattern.reward === "gift")
      what = Number(giftToken) > 0 ? `gift at ₹${giftToken}` : "free gift";
    if (pattern.trigger === "spend" && steps[0]) what = `spend ₹${steps[0].min} → ${what}`;
    if (pattern.trigger === "qty" && steps[0]) what = `buy ${steps[0].min}+ → ${what}`;
    return `${where}: ${what}`;
  }, [brands, brand, pattern, percent, amount, price, buy, get, giftToken, steps]);

  function body(): Record<string, unknown> {
    const rewardConfig: Record<string, unknown> = {};
    if (pattern.asks.includes("percent")) rewardConfig.percent = Number(percent).toFixed(2);
    if (pattern.asks.includes("amount")) rewardConfig.amount_paise = Math.round(Number(amount) * 100);
    if (pattern.asks.includes("price")) rewardConfig.price_paise = Math.round(Number(price) * 100);
    if (pattern.asks.includes("gift")) {
      rewardConfig.gift_barcode = giftBarcode.trim();
      rewardConfig.token_price_paise = Math.round(Number(giftToken || 0) * 100);
    }
    if (pattern.reward === "item_free") rewardConfig.allocation = allocation;
    if (layer === "bank") rewardConfig.reduces_value = reducesValue;
    const triggerConfig: Record<string, unknown> = {};
    if (pattern.asks.includes("buyget")) {
      triggerConfig.buy = Number(buy);
      triggerConfig.get = Number(get);
    }
    if (pattern.asks.includes("qtySlabs") || pattern.asks.includes("spendSlabs")) {
      const money = pattern.asks.includes("spendSlabs");
      triggerConfig.slabs = steps.map((step) => {
        const slab: Record<string, unknown> = money
          ? { min_paise: Math.round(Number(step.min) * 100) }
          : { min_qty: Number(step.min) };
        // The reward's amount belongs to the step it is earned at; the *type*
        // belongs to the rule. A ladder whose top step forgot its value pays
        // nothing to the biggest spender.
        if (pattern.reward === "pct_off") slab.percent = Number(step.value).toFixed(2);
        if (pattern.reward === "amt_off") slab.amount_paise = Math.round(Number(step.value) * 100);
        return slab;
      });
    }
    const itemScope: Record<string, unknown> = {};
    if (mrpMin) itemScope.mrp_min_paise = Math.round(Number(mrpMin) * 100);
    if (mrpMax) itemScope.mrp_max_paise = Math.round(Number(mrpMax) * 100);
    return {
      name: name.trim(),
      layer,
      funder,
      brand: layer === "brand" ? brand : null,
      trigger_type: pattern.trigger,
      trigger_config: triggerConfig,
      reward_type: pattern.reward,
      reward_config: rewardConfig,
      item_scope: itemScope,
      store_scope: scopeAll
        ? { kind: "all", stores: stores.map((s) => s.code) }
        : { kind: "specific", stores: picked },
      starts_on: startsOn,
      ends_on: endsOn || null,
      combinable,
      priority: Number(priority),
    };
  }

  async function save() {
    setSaving(true);
    setError("");
    setNote("");
    try {
      const r = isNew
        ? await api.post("/offers/", body())
        : await api.put(`/offers/${id}`, body());
      setOffer(r.data);
      setNote("Saved as a draft. Send it for approval when it reads right.");
      if (isNew) nav(`/offers/${r.data.id}`, { replace: true });
    } catch (e) {
      setError(apiErrorMessage(e));
    } finally {
      setSaving(false);
    }
  }

  async function sendForApproval() {
    setSaving(true);
    setError("");
    try {
      const r = await api.post(`/offers/${id}/request-approval`, {});
      setOffer(r.data);
      setNote("Sent. It appears on Action Needed for whoever answers for this brand.");
    } catch (e) {
      setError(apiErrorMessage(e));
    } finally {
      setSaving(false);
    }
  }

  async function stop() {
    setSaving(true);
    setError("");
    try {
      const r = await api.put(`/offers/${id}`, { status: "ended" });
      setOffer(r.data);
      setNote("Stopped. Bills already priced under it keep their discount.");
    } catch (e) {
      setError(apiErrorMessage(e));
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="page-pad">
      <PageHeader
        title={isNew ? "New offer" : "Edit offer"}
        lead={
          isNew
            ? "Write a rule the till can price with. It starts as a draft; a second person clears it before a single bill reads it."
            : "The rule as it stands. A running rule is stopped and rewritten, never edited - yesterday's bill has to stay explicable by the rule that priced it."
        }
      />

      <div className="toolbar">
        <Link className="crumb-link" to="/offers" data-testid="offer-back">
          ← All offers
        </Link>
        <span className="spacer" />
        {offer && (
          <span className={`chip chip-${offer.status === "live" ? "green" : offer.status === "ended" ? "grey" : "amber"}`} data-testid="offer-status">
            {offer.awaiting_approval ? "waiting for approval" : offer.status}
          </span>
        )}
      </div>

      {error && (
        <div className="warn-note" data-testid="offer-error">
          {error}
        </div>
      )}
      {note && (
        <div className="ai-note" data-testid="offer-note">
          {note}
        </div>
      )}

      {offer && (
        <div className="card section-card" data-testid="offer-asis">
          <p className="eyebrow">
            <Tag size={14} /> As the shop will read it
          </p>
          <p className="h2-rust" style={{ margin: "4px 0 0" }}>
            {offer.one_liner}
          </p>
          <p className="hint">
            {offer.stores.length} store{offer.stores.length === 1 ? "" : "s"} ·{" "}
            {offer.approved_by_name ? `cleared by ${offer.approved_by_name}` : "not yet cleared"}
          </p>
        </div>
      )}

      <div className="card section-card">
        <p className="eyebrow">What kind of offer</p>
        <div className="pattern-grid">
          {PATTERNS.map((p) => (
            <button
              key={p.key}
              type="button"
              disabled={frozen}
              className={`mode-btn${pattern.key === p.key ? " active" : ""}`}
              onClick={() => setPattern(p)}
              data-testid={`offer-pattern-${p.key}`}
            >
              <span>
                <b>{p.label}</b>
                <span className="pattern-hint">{p.hint}</span>
              </span>
            </button>
          ))}
        </div>
      </div>

      <div className="card section-card">
        <p className="eyebrow">The rule</p>
        <div className="form-row">
          <label className="field">
            Name a store manager can read
            <input
              className="input"
              value={name}
              disabled={frozen}
              onChange={(e) => setName(e.target.value)}
              placeholder="Peter England SS26 - flat 20%"
              data-testid="offer-name"
            />
          </label>
          <label className="field">
            Layer
            <select
              className="select"
              value={layer}
              disabled={frozen}
              onChange={(e) => setLayer(e.target.value)}
              data-testid="offer-layer"
            >
              <option value="brand">Brand offer</option>
              <option value="storewide">Storewide add-on</option>
              <option value="bank">Bank / tender add-on</option>
            </select>
          </label>
          <label className="field">
            Brand
            <select
              className="select"
              value={brand}
              disabled={frozen || layer !== "brand"}
              onChange={(e) => setBrand(e.target.value)}
              data-testid="offer-brand"
            >
              <option value="">
                {layer === "brand" ? "Pick the brand" : "Not a brand rule"}
              </option>
              {brands.map((b) => (
                <option key={b.id} value={b.code}>
                  {b.name}
                </option>
              ))}
            </select>
          </label>
          <label className="field">
            Whose discount is it
            <select
              className="select"
              value={funder}
              disabled={frozen}
              onChange={(e) => setFunder(e.target.value)}
              data-testid="offer-funder"
            >
              <option value="brand">The brand's own scheme</option>
              <option value="kdps">KDPS's own markdown</option>
            </select>
          </label>
        </div>

        <div className="form-row" style={{ marginTop: 14 }}>
          {pattern.asks.includes("percent") && (
            <label className="field">
              Percentage off
              <input
                className="input num"
                value={percent}
                disabled={frozen}
                onChange={(e) => setPercent(e.target.value)}
                data-testid="offer-percent"
              />
            </label>
          )}
          {pattern.asks.includes("amount") && (
            <label className="field">
              Rupees off
              <input
                className="input num"
                value={amount}
                disabled={frozen}
                onChange={(e) => setAmount(e.target.value)}
                data-testid="offer-amount"
              />
            </label>
          )}
          {pattern.asks.includes("price") && (
            <label className="field">
              Sells at ₹
              <input
                className="input num"
                value={price}
                disabled={frozen}
                onChange={(e) => setPrice(e.target.value)}
                data-testid="offer-price"
              />
            </label>
          )}
          {pattern.asks.includes("buyget") && (
            <>
              <label className="field">
                Buy
                <input
                  className="input num"
                  value={buy}
                  disabled={frozen}
                  onChange={(e) => setBuy(e.target.value)}
                  data-testid="offer-buy"
                />
              </label>
              <label className="field">
                Get free
                <input
                  className="input num"
                  value={get}
                  disabled={frozen}
                  onChange={(e) => setGet(e.target.value)}
                  data-testid="offer-get"
                />
              </label>
            </>
          )}
          {pattern.asks.includes("gift") && (
            <>
              <label className="field">
                Gift barcode
                <input
                  className="input"
                  value={giftBarcode}
                  disabled={frozen}
                  onChange={(e) => setGiftBarcode(e.target.value)}
                  data-testid="offer-gift-barcode"
                />
              </label>
              <label className="field">
                Token price ₹ (nil for free)
                <input
                  className="input num"
                  value={giftToken}
                  disabled={frozen}
                  onChange={(e) => setGiftToken(e.target.value)}
                  data-testid="offer-gift-token"
                />
              </label>
            </>
          )}
        </div>

        {(pattern.asks.includes("qtySlabs") || pattern.asks.includes("spendSlabs")) && (
          <div style={{ marginTop: 16 }}>
            <p className="eyebrow">
              The ladder — {pattern.asks.includes("spendSlabs") ? "spend" : "pieces"}, and what it earns
            </p>
            {steps.map((step, index) => (
              <div className="eoss-ladder-row" key={index}>
                <span className="hint" style={{ minWidth: 46 }}>
                  {pattern.asks.includes("spendSlabs") ? "₹ from" : "pieces"}
                </span>
                <input
                  className="input num"
                  value={step.min}
                  disabled={frozen}
                  onChange={(e) =>
                    setSteps(steps.map((s, i) => (i === index ? { ...s, min: e.target.value } : s)))
                  }
                  data-testid={`offer-step-min-${index}`}
                />
                <span className="hint">earns</span>
                <input
                  className="input num"
                  value={step.value}
                  disabled={frozen}
                  onChange={(e) =>
                    setSteps(
                      steps.map((s, i) => (i === index ? { ...s, value: e.target.value } : s)),
                    )
                  }
                  data-testid={`offer-step-value-${index}`}
                />
                <span className="hint">{pattern.reward === "pct_off" ? "%" : "₹"}</span>
                {steps.length > 1 && !frozen && (
                  <button
                    type="button"
                    className="line-del"
                    onClick={() => setSteps(steps.filter((_, i) => i !== index))}
                    data-testid={`offer-step-del-${index}`}
                  >
                    ✕
                  </button>
                )}
              </div>
            ))}
            {!frozen && (
              <button
                type="button"
                className="btn"
                onClick={() => setSteps([...steps, { min: "", value: "" }])}
                data-testid="offer-step-add"
              >
                + another step
              </button>
            )}
          </div>
        )}
      </div>

      <div className="card section-card">
        <p className="eyebrow">Where, when, and how it behaves</p>
        <div className="filter-bar">
          <button
            type="button"
            className={`chip chip-pick${scopeAll ? " chip-navy" : ""}`}
            disabled={frozen}
            onClick={() => setScopeAll(true)}
            data-testid="offer-scope-all"
          >
            Every store
          </button>
          <button
            type="button"
            className={`chip chip-pick${!scopeAll ? " chip-navy" : ""}`}
            disabled={frozen}
            onClick={() => setScopeAll(false)}
            data-testid="offer-scope-pick"
          >
            Only the ones I pick
          </button>
        </div>
        {!scopeAll && (
          <div className="filter-bar" data-testid="offer-store-picker">
            {stores.map((s) => (
              <button
                key={s.id}
                type="button"
                disabled={frozen}
                className={`chip chip-pick${picked.includes(s.code) ? " chip-navy" : ""}`}
                onClick={() =>
                  setPicked(
                    picked.includes(s.code)
                      ? picked.filter((c) => c !== s.code)
                      : [...picked, s.code],
                  )
                }
                data-testid={`offer-store-${s.code}`}
              >
                {s.code}
              </button>
            ))}
          </div>
        )}
        <p className="hint">
          Stores are resolved the day the offer is written, so a shop opened next month is never
          enrolled behind your back.
        </p>

        <div className="form-row" style={{ marginTop: 14 }}>
          <label className="field">
            Runs from
            <input
              type="date"
              className="input"
              value={startsOn}
              disabled={frozen}
              onChange={(e) => setStartsOn(e.target.value)}
              data-testid="offer-starts"
            />
          </label>
          <label className="field">
            Until (blank = until stopped)
            <input
              type="date"
              className="input"
              value={endsOn}
              disabled={frozen}
              onChange={(e) => setEndsOn(e.target.value)}
              data-testid="offer-ends"
            />
          </label>
          <label className="field">
            Priority (lower wins)
            <input
              className="input num"
              value={priority}
              disabled={frozen}
              onChange={(e) => setPriority(e.target.value)}
              data-testid="offer-priority"
            />
          </label>
          <label className="field">
            Only pieces ticketed ₹ from / to
            <div style={{ display: "flex", gap: 8 }}>
              <input
                className="input num"
                value={mrpMin}
                disabled={frozen}
                onChange={(e) => setMrpMin(e.target.value)}
                placeholder="any"
                data-testid="offer-mrp-min"
              />
              <input
                className="input num"
                value={mrpMax}
                disabled={frozen}
                onChange={(e) => setMrpMax(e.target.value)}
                placeholder="any"
                data-testid="offer-mrp-max"
              />
            </div>
          </label>
        </div>
        <label className="field" style={{ marginTop: 12 }}>
          <span>
            <input
              type="checkbox"
              checked={combinable}
              disabled={frozen}
              onChange={(e) => setCombinable(e.target.checked)}
              data-testid="offer-combinable"
            />{" "}
            Stacks on top of another offer
          </span>
          <span className="hint">
            Left off, this is the only discount the line takes. "Can I give the bank offer on top of
            the brand one?" is the question a counter actually asks, and this is the answer.
          </span>
        </label>
        {pattern.reward === "item_free" && (
          <label className="field" style={{ marginTop: 12 }}>
            Tax: how the price is shared
            <select
              className="select"
              value={allocation}
              disabled={frozen}
              onChange={(e) => setAllocation(e.target.value)}
              data-testid="offer-allocation"
            >
              <option value="by_mrp">Spread over every piece by MRP</option>
              <option value="free_piece">The free piece carries the whole discount</option>
            </select>
            <span className="hint">
              Where GST after discount is switched on. Spread by MRP, each piece is taxed on its own
              share of the price (baseline, CA to confirm).
            </span>
          </label>
        )}
        {layer === "bank" && (
          <label className="field" style={{ marginTop: 12 }}>
            <span>
              <input
                type="checkbox"
                checked={reducesValue}
                disabled={frozen}
                onChange={(e) => setReducesValue(e.target.checked)}
                data-testid="offer-reduces-value"
              />{" "}
              Reduces the taxable value
            </span>
            <span className="hint">
              Where GST after discount is switched on. Left off, the bank discount is recorded as a
              "bank offer" payment and the piece is taxed on its full price (baseline, CA to
              confirm).
            </span>
          </label>
        )}
      </div>

      <div className="card section-card">
        <p className="eyebrow">Reads as</p>
        <p className="h2-rust" style={{ margin: "2px 0 10px" }} data-testid="offer-preview">
          {preview}
        </p>
        <p className="hint">
          A draft reading, assembled here. The phrase the shop sees is generated by the server off
          the stored rule, and it is the one shown above once this is saved.
        </p>
        <div className="filter-bar" style={{ marginTop: 14 }}>
          {!frozen && (
            <button
              type="button"
              className="btn btn-primary btn-lg"
              disabled={saving}
              onClick={save}
              data-testid="offer-save"
            >
              <Check size={16} /> {isNew ? "Save as draft" : "Save changes"}
            </button>
          )}
          {offer && offer.status === "draft" && !offer.awaiting_approval && (
            <button
              type="button"
              className="btn btn-lg"
              disabled={saving}
              onClick={sendForApproval}
              data-testid="offer-send"
            >
              <Send size={16} /> Send for approval
            </button>
          )}
          {offer && offer.status === "live" && (
            <button
              type="button"
              className="btn btn-lg"
              disabled={saving}
              onClick={stop}
              data-testid="offer-stop"
            >
              <CircleSlash size={16} /> Stop this offer
            </button>
          )}
        </div>
        {offer?.awaiting_approval && (
          <p className="hint" data-testid="offer-waiting">
            <CalendarClock size={13} /> Waiting for a second person. You cannot clear your own
            offer — that is the point of the gate.
          </p>
        )}
      </div>

      {offer && (
        // Ticket 30: what the offer would have cost on real past bills, before
        // it is approved. Hidden where the switch is off.
        <OfferSimulationCard offerId={offer.id} status={offer.status} updatedAt={offer.updated_at} />
      )}
      {offer && (offer.status === "live" || offer.status === "ended") && (
        // Ticket 31: what the offer earned and cost once it ran, against a
        // baseline. Hidden where the switch is off or it has not started.
        <OfferReturnCard offerId={offer.id} />
      )}
    </div>
  );
}

export default OfferAuthorPage;
