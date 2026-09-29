import { useCallback, useMemo, useState } from "react";

/**
 * "Counter Plus" preview: screens only, made-up data, nothing saved.
 *
 * Loyalty, credit billing (udhaar), bank transfer, cheque, store credit and a
 * bill-level discount are not in the PRD (or not built), so no rule here is a
 * product decision: the numbers are placeholders for Anand to change. The
 * preview never touches the cart, the price engine or the till.
 */

/** Placeholder earn rate: 1 point for every ₹100 paid. */
export const PAISE_PER_POINT_EARNED = 10000;
/** Placeholder value: 1 point = ₹0.10. */
export const PAISE_PER_POINT_VALUE = 10;

export type Tier = "Silver" | "Gold";

export type PlusCustomer = {
  name: string;
  mobile: string;
  tier: Tier;
  points: number;
  creditLimitPaise: number;
  creditUsedPaise: number;
  storeCreditPaise: number;
  savedSize: string | null;
};

export const DEMO_CUSTOMERS: PlusCustomer[] = [
  {
    name: "Ravi Kumar",
    mobile: "9000000001",
    tier: "Gold",
    points: 1240,
    creditLimitPaise: 2000000,
    creditUsedPaise: 650000,
    storeCreditPaise: 50000,
    savedSize: "Shirt 40",
  },
  {
    name: "Meena Devi",
    mobile: "9000000002",
    tier: "Silver",
    points: 320,
    creditLimitPaise: 500000,
    creditUsedPaise: 480000,
    storeCreditPaise: 0,
    savedSize: "Kurta M",
  },
];

export const DUE_DAY_CHOICES = [7, 15, 30] as const;

export function pointsEarned(paidPaise: number): number {
  return Math.max(0, Math.floor(paidPaise / PAISE_PER_POINT_EARNED));
}

export function pointsValuePaise(points: number): number {
  return points * PAISE_PER_POINT_VALUE;
}

/** The most points that can be used: what the customer has, and no more than
 *  the bill. */
export function maxPointsFor(customer: PlusCustomer | null, payablePaise: number): number {
  if (!customer) return 0;
  return Math.max(0, Math.min(customer.points, Math.floor(payablePaise / PAISE_PER_POINT_VALUE)));
}

export function creditRoomPaise(customer: PlusCustomer): number {
  return Math.max(0, customer.creditLimitPaise - customer.creditUsedPaise);
}

/** "What, why, what to do instead" (PRD §28), or null when the credit is fine. */
export function creditRefusal(
  customer: PlusCustomer | null,
  creditPaise: number,
  pinOk: boolean,
): string | null {
  if (creditPaise <= 0) return null;
  if (!customer) {
    return "Credit needs a registered customer, so the shop knows who owes. Add the customer, or take another payment.";
  }
  const room = creditRoomPaise(customer);
  if (creditPaise > room && !pinOk) {
    return "This is over the credit limit. Ask a manager to approve with their PIN, or take part of it another way.";
  }
  return null;
}

export function normaliseMobile(text: string): string {
  return text.replace(/\D/g, "").slice(-10);
}

export type MoreMode = "bank" | "cheque" | "storeCredit" | "credit";

export type PlusState = {
  customer: PlusCustomer | null;
  registering: boolean;
  usePointsCount: number;
  bankPaise: number;
  chequePaise: number;
  storeCreditPaise: number;
  creditPaise: number;
  dueDays: number;
  pinOk: boolean;
  discountPaise: number;
  openModes: MoreMode[];
  receiveOpen: boolean;
};

const START: PlusState = {
  customer: null,
  registering: false,
  usePointsCount: 0,
  bankPaise: 0,
  chequePaise: 0,
  storeCreditPaise: 0,
  creditPaise: 0,
  dueDays: 15,
  pinOk: false,
  discountPaise: 0,
  openModes: [],
  receiveOpen: false,
};

export function usePlusPreview() {
  const [state, setState] = useState<PlusState>(START);
  const patch = useCallback((p: Partial<PlusState>) => setState((s) => ({ ...s, ...p })), []);
  const pickCustomer = useCallback(
    (customer: PlusCustomer | null) =>
      setState((s) => ({
        ...s,
        customer,
        registering: false,
        usePointsCount: 0,
        storeCreditPaise: 0,
        creditPaise: 0,
        pinOk: false,
      })),
    [],
  );
  const reset = useCallback(() => setState(START), []);
  return useMemo(
    () => ({ state, patch, pickCustomer, reset }),
    [state, patch, pickCustomer, reset],
  );
}

export type Plus = ReturnType<typeof usePlusPreview>;
