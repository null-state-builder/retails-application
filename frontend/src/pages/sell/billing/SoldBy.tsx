/** The bill-level "Sold by" default, second in the payment rail after the
 * customer (design system v2 TillLayout). New lines take this person; a line's
 * own salesperson is changed on the line. It owns no state: Billing keeps the
 * existing actor safeguards. */
export function SoldBy({
  salespeople,
  soldBy,
  differingLines,
  disabled,
  onChange,
  onApply,
}: {
  salespeople: { id: string; name: string }[];
  soldBy: string | null;
  differingLines: number;
  disabled: boolean;
  onChange: (salesperson: string | null) => void;
  onApply: () => void;
}) {
  return (
    <section className="bill-rail-section bill-sold-by">
      <label className="bill-sold-by-field">
        <span className="eyebrow">Sold by</span>
        <select
          className="select"
          data-testid="bill-sold-by"
          disabled={disabled}
          value={soldBy ?? ""}
          onChange={(event) => onChange(event.target.value || null)}
        >
          <option value="">Nobody</option>
          {salespeople.map((salesperson) => (
            <option key={salesperson.id} value={salesperson.id}>
              {salesperson.name}
            </option>
          ))}
        </select>
      </label>
      {differingLines > 0 && soldBy !== null && (
        <button
          type="button"
          className="btn btn-sm bill-apply-sold-by"
          data-testid="bill-apply-sold-by"
          title="Apply the bill-level Sold by choice to every line"
          disabled={disabled}
          onClick={onApply}
        >
          Apply to all {differingLines} {differingLines === 1 ? "line" : "lines"}
        </button>
      )}
    </section>
  );
}
