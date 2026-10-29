import { describe, expect, it } from "vitest";

import { filterCards, type BookingCard } from "./bookingModel";

function card(id: string, status: BookingCard["status"], createdAt: string): BookingCard {
  return {
    engine: "legacy",
    id,
    number: `BK-${id}`,
    status,
    brand: "Spykar",
    vendor: "Spykar Lifestyles",
    season: "SS27",
    store: "WRR",
    booked: 10,
    arrived: 0,
    expected: null,
    createdAt,
  };
}

describe("the bookings list's chips", () => {
  const cards = [
    card("1", "waiting", "2026-10-01"),
    card("2", "booked", "2026-10-02"),
    card("3", "all", "2026-10-03"),
  ];

  it("counts every chip over the same searched list, waiting inside open", () => {
    expect(filterCards(cards, "open", "").counts).toEqual({
      open: 2,
      waiting: 1,
      done: 1,
      all: 3,
    });
  });

  it("shows only the bookings waiting for approval on that chip, newest first", () => {
    expect(filterCards(cards, "waiting", "").shown.map((c) => c.id)).toEqual(["1"]);
    expect(filterCards(cards, "all", "").shown.map((c) => c.id)).toEqual(["3", "2", "1"]);
  });
});
