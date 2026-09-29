/** Scheduled counts (store operations ticket 35, ST-INV-3): the words and the
 *  links the Count Schedule page, Today and the count-due alert share. The
 *  server decides what is due and what was missed; this only says it. */

export const COUNT_SCHEDULE_PATH = "/stock-count/schedule";

const EVERY_TEXT: Record<string, string> = {
  week: "Every week",
  month: "Every month",
  quarter: "Every 3 months",
};

export const EVERY_CHOICES = Object.entries(EVERY_TEXT).map(([value, label]) => ({
  value,
  label,
}));

export function everyText(every: string): string {
  return EVERY_TEXT[every] ?? every;
}

/** What is counted: a brand, or the whole store (a null brand). */
export function scopeText(brandName: string | null | undefined): string {
  return brandName || "Whole store";
}

/** The store's existing blind count: the goods-v1 count for a site on the goods
 *  records (with that site chosen), else the older count sessions. */
export function countScreenPath(site: { id: number; goods_v1: boolean }): string {
  return site.goods_v1 ? `/goods/counts?site=${site.id}` : "/stock-count";
}

/** Where a count-due alert opens: this page, showing that store. */
export function alertCountSchedulePath(storeId: number): string {
  return `${COUNT_SCHEDULE_PATH}?site_id=${storeId}`;
}
