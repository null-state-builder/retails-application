// The three small pieces Action Needed and Alerts both draw (23 Sep 2026):
// a topic chip, an urgency badge, and a row of topic filter buttons. Their
// words and icons come from `lib/actionCatalogue.ts`; only the drawing is here.
import { TOPICS, TOPIC_ORDER, type Topic } from "../lib/actionCatalogue";
import "./ActionBits.css";

/** Which part of the business a thing is about: icon and word, no colour —
 *  colour on these screens means urgency. */
export function TopicChip({ topic }: { topic: Topic }) {
  const { label, icon: Icon } = TOPICS[topic];
  return (
    <span className="an-topic" data-topic={topic}>
      <Icon size={13} aria-hidden /> {label}
    </span>
  );
}

export type Urgency = "late" | "today" | "later";

/** How urgent, in the house status colours: red past due, amber due today,
 *  grey otherwise. Always a word in it, never colour alone. */
export function UrgencyBadge({
  urgency,
  children,
  testId,
}: {
  urgency: Urgency;
  children: string;
  testId?: string;
}) {
  return (
    <span className={`an-due an-due-${urgency}`} data-testid={testId}>
      {children}
    </span>
  );
}

/** "All" plus one button per topic that has something in it (and the chosen
 *  one, even once it has emptied, so it can still be turned off). */
export function TopicFilter({
  counts,
  total,
  value,
  onChange,
  label,
  testId,
  itemTestId,
}: {
  counts: ReadonlyMap<Topic, number>;
  total: number;
  value: Topic | "";
  onChange: (next: Topic | "") => void;
  /** Read out as the group's name. */
  label: string;
  testId: string;
  /** Prefix for each button's test id: `${itemTestId}-all`, `${itemTestId}-receiving`… */
  itemTestId: string;
}) {
  const shown = TOPIC_ORDER.filter((t) => (counts.get(t) ?? 0) > 0 || t === value);
  return (
    <div className="an-topics" role="group" aria-label={label} data-testid={testId}>
      <button
        type="button"
        className={`btn btn-sm${value === "" ? " btn-active" : ""}`}
        aria-pressed={value === ""}
        onClick={() => onChange("")}
        data-testid={`${itemTestId}-all`}
      >
        All <span className="an-count">{total}</span>
      </button>
      {shown.map((t) => {
        const { label: name, icon: Icon } = TOPICS[t];
        return (
          <button
            key={t}
            type="button"
            className={`btn btn-sm${value === t ? " btn-active" : ""}`}
            aria-pressed={value === t}
            onClick={() => onChange(value === t ? "" : t)}
            data-testid={`${itemTestId}-${t}`}
          >
            <Icon size={13} aria-hidden /> {name}{" "}
            <span className="an-count">{counts.get(t) ?? 0}</span>
          </button>
        );
      })}
    </div>
  );
}

/** Count rows by topic, for a `TopicFilter`. */
export function countTopics<T>(rows: readonly T[], topicOf: (row: T) => Topic): Map<Topic, number> {
  const counts = new Map<Topic, number>();
  for (const row of rows) {
    const t = topicOf(row);
    counts.set(t, (counts.get(t) ?? 0) + 1);
  }
  return counts;
}

/** A topic read back from a URL, or "" when it names none. */
export function parseTopic(value: string | null): Topic | "" {
  return value && value in TOPICS ? (value as Topic) : "";
}
