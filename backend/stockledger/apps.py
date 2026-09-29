from django.apps import AppConfig


class StockledgerConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "stockledger"

    def ready(self) -> None:
        # The goods-v1 operational inventory adapter behind
        # `core.posting.post_entries(ledger="operational_inventory")` (design §7.1).
        # Registered once, in code; nothing user-editable can replace it.
        from stockledger.goods_engine import install

        install()

        # The `stock_csv` durable export (GSA-T18): the stock reader renders its
        # own rows as a file, so screen and export cannot drift apart.
        from files.goods_exports import register_export_kind
        from stockledger import goods_export_stock

        register_export_kind(
            goods_export_stock.KIND,
            check=goods_export_stock.check,
            produce=goods_export_stock.produce,
        )
