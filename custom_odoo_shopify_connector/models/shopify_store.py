from datetime import timedelta

from odoo import api, fields, models, _
from odoo.exceptions import UserError
from ..services.shopify_api import ShopifyAPI


class ShopifyStore(models.Model):
    _name = "shopify.store"
    _description = "Shopify Store"

    name = fields.Char(required=True)
    shop_url = fields.Char(
        required=True,
        help="Base URL of your Shopify store, for example: https://your-store.myshopify.com",
    )
    access_token = fields.Char(
        help="Admin API access token generated from your Shopify private/custom app.",
    )
    api_key = fields.Char(
        help="Shopify API key (Client ID).",
    )
    api_secret = fields.Char(
        help="Shopify API secret key.",
    )
    webhook_secret = fields.Char()
    active = fields.Boolean(default=True)
    shopify_location_id = fields.Char(
        string="Default Shopify Location ID",
        help="Location used for inventory synchronization.",
    )
    company_id = fields.Many2one(
        "res.company",
        string="Company",
        default=lambda self: self.env.company.id,
    )
    default_unshipped_order_warehouse_id = fields.Many2one(
        "stock.warehouse",
        string="Default Warehouse for Unshipped Orders",
        help="Warehouse used when importing unshipped Shopify orders into Odoo.",
    )
    sale_auto_workflow_id = fields.Many2one(
        "shopify.sale.auto.workflow",
        string="Sales Auto Workflow",
        help="Sales automation workflow to apply to imported Shopify orders.",
    )

    delivery_product_id = fields.Many2one(
        "product.product",
        string="Delivery Product",
        help="Product used when creating shipping lines from Shopify orders.",
    )

    # Order import configuration
    import_order_status = fields.Selection(
        [
            ("unshipped", "Unshipped (fulfillment_status = null)"),
            (
                "partially_fulfilled",
                "Unshipped + Partially Fulfilled (fulfillment_status = null or partial)",
            ),
        ],
        string="Import Orders With",
        default="unshipped",
        help="Controls which Shopify orders are imported based on their fulfillment status.",
    )
    use_odoo_sequence = fields.Boolean(
        string="Use Odoo Sequence",
        help="If enabled, use the standard Odoo sale order sequence instead of the Shopify order number.",
    )
    order_prefix = fields.Char(
        string="Order Prefix",
        help='Optional prefix for Shopify order numbers used as Odoo order names, e.g. "SHP-".',
    )
    default_pos_customer_id = fields.Many2one(
        "res.partner",
        string="Default POS Customer",
        help="If a Shopify POS order has no customer, this partner will be used on the sale order.",
    )
    auto_fulfill_gift_card = fields.Boolean(
        string="Automatically Fulfill Gift Card",
        help="If enabled, Shopify gift card products will be automatically marked as delivered.",
    )
    shopify_tax_behavior = fields.Selection(
        [
            ("create_tax_if_not_found", "Create New Tax If Not Found"),
            ("use_odoo_tax", "Use Odoo Default Tax"),
        ],
        string="Tax Behavior",
        default="create_tax_if_not_found",
        help="Controls how Shopify taxes are mapped to Odoo taxes during order import.",
    )
    last_order_import_time = fields.Datetime(
        string="Last Order Import Time",
        help="Timestamp of the last successful order fetch from Shopify (used by scheduler).",
    )
    manage_orders_webhook = fields.Boolean(
        string="Manage Orders via Webhook",
        help="If enabled, Shopify order webhooks will be used to import and update orders.",
    )

    # Update Order Shipping Status (Odoo → Shopify) scheduler
    update_shipping_sync_enabled = fields.Boolean(
        string="Update Order Shipping Status",
        default=False,
        help="If enabled, completed deliveries will be synced to Shopify at the configured interval.",
    )
    update_shipping_interval_number = fields.Integer(
        string="Shipping Sync Interval",
        default=30,
        help="Interval between automatic shipping status updates to Shopify.",
    )
    update_shipping_interval_type = fields.Selection(
        [
            ("minutes", "Minutes"),
            ("hours", "Hours"),
        ],
        string="Interval Unit",
        default="minutes",
    )
    last_shipping_sync_time = fields.Datetime(
        string="Last Shipping Sync",
        readonly=True,
        help="Last time the shipping status sync ran for this store.",
    )

    # Import Shipped Orders scheduler (Shopify fulfilled orders → Odoo)
    import_shipped_orders_enabled = fields.Boolean(
        string="Import Shipped Orders",
        default=False,
        help="If enabled, fulfilled Shopify orders will be imported into Odoo on the configured interval.",
    )
    import_shipped_orders_interval_number = fields.Integer(
        string="Shipped Orders Interval",
        default=25,
        help="Interval between automatic imports of fulfilled Shopify orders.",
    )
    import_shipped_orders_interval_type = fields.Selection(
        [("minutes", "Minutes"), ("hours", "Hours")],
        string="Interval Unit",
        default="minutes",
    )
    last_shipped_orders_import_time = fields.Datetime(
        string="Last Shipped Orders Import",
        readonly=True,
        help="Last time fulfilled orders were imported for this store.",
    )

    orders_imported_count = fields.Integer(
        string="Orders Imported (24h)",
        compute="_compute_order_stats",
    )
    orders_failed_count = fields.Integer(
        string="Orders Failed (24h)",
        compute="_compute_order_stats",
    )
    orders_pending_count = fields.Integer(
        string="Orders Pending (24h)",
        compute="_compute_order_stats",
    )

    def _compute_order_stats(self):
        OrderQueue = self.env["shopify.order.queue"]
        now = fields.Datetime.now()
        # last 24 hours window
        yesterday = now - timedelta(days=1)
        for store in self:
            imported = OrderQueue.search_count(
                [
                    ("store_id", "=", store.id),
                    ("state", "=", "done"),
                    ("create_date", ">=", yesterday),
                ]
            )
            failed = OrderQueue.search_count(
                [
                    ("store_id", "=", store.id),
                    ("state", "=", "failed"),
                    ("create_date", ">=", yesterday),
                ]
            )
            store.orders_imported_count = imported
            store.orders_failed_count = failed
            store.orders_pending_count = OrderQueue.search_count(
                [
                    ("store_id", "=", store.id),
                    ("state", "=", "pending"),
                    ("create_date", ">=", yesterday),
                ]
            )

    def import_orders_scheduler(self):
        """Cron entry point: fetch new Shopify orders per active store and
        enqueue them for processing based on instance configuration.
        """
        from ..services.queue_service import _shopify_datetime
        from ..services.order_import_service import OrderImportService
        import json

        stores = self.search([("active", "=", True)])
        import_service = OrderImportService(self.env)

        for store in stores:
            api_client = store._get_api_client()
            params = {}
            if store.last_order_import_time:
                params["created_at_min"] = _shopify_datetime(store.last_order_import_time)

            try:
                orders = api_client.get_orders(**params)
            except Exception as e:
                self.env["shopify.sync.log.mixin"].create_log(
                    store=store,
                    log_type="order",
                    message=str(e),
                    payload=False,
                    status="failed",
                )
                continue

            Queue = self.env["shopify.order.queue"]
            for order in orders:
                if not import_service.should_import_order(store, order):
                    continue
                Queue.create(
                    {
                        "store_id": store.id,
                        "shopify_order_id": str(order.get("id") or ""),
                        "payload": json.dumps(order),
                        "state": "pending",
                        "job_type": "order",
                    }
                )

            store.last_order_import_time = fields.Datetime.now()

    def cron_shopify_update_shipping_status(self):
        """Cron entry point: sync shipping status to Shopify for completed deliveries.
        Runs for each store where sync is enabled and the configured interval has passed.
        """
        from ..services.shipping_service import ShopifyShippingService

        now = fields.Datetime.now()
        stores = self.search([("active", "=", True), ("update_shipping_sync_enabled", "=", True)])
        if not stores:
            return

        shipping_service = ShopifyShippingService(self.env)
        Picking = self.env["stock.picking"]

        for store in stores:
            num = store.update_shipping_interval_number or 0
            if num <= 0:
                continue
            itype = store.update_shipping_interval_type or "minutes"
            interval_delta = timedelta(minutes=num) if itype == "minutes" else timedelta(hours=num)
            if interval_delta.total_seconds() <= 0:
                continue
            last_run = store.last_shipping_sync_time or fields.Datetime.from_string("1970-01-01 00:00:00")
            if (now - last_run) < interval_delta:
                continue

            sale_orders = self.env["sale.order"].search([("shopify_instance_id", "=", store.id)])
            if not sale_orders:
                store.write({"last_shipping_sync_time": now})
                continue

            origins = sale_orders.mapped("name")
            pickings = Picking.search(
                [
                    ("state", "=", "done"),
                    ("shopify_shipping_status", "=", "pending"),
                    ("origin", "in", origins),
                ]
            )
            for picking in pickings:
                sale = picking._get_shopify_sale_order()
                if sale and sale.shopify_order_id and sale.shopify_instance_id == store:
                    shipping_service.update_shipping(picking, sale, store)

            store.write({"last_shipping_sync_time": now})

    def cron_shopify_import_shipped_orders(self):
        """Cron entry point: automatically import fulfilled orders into Odoo.

        Creates an import queue (date window since last run) and processes it.
        """
        from datetime import timedelta

        now = fields.Datetime.now()
        stores = self.search([("active", "=", True), ("import_shipped_orders_enabled", "=", True)])
        if not stores:
            return

        service = self.env["shopify.service"]
        Queue = self.env["shopify.import.queue"]

        for store in stores:
            num = store.import_shipped_orders_interval_number or 0
            if num <= 0:
                continue
            itype = store.import_shipped_orders_interval_type or "minutes"
            interval_delta = timedelta(minutes=num) if itype == "minutes" else timedelta(hours=num)
            if interval_delta.total_seconds() <= 0:
                continue

            last_run = store.last_shipped_orders_import_time or fields.Datetime.from_string("1970-01-01 00:00:00")
            if (now - last_run) < interval_delta:
                continue

            start = last_run
            end = now
            queue = service.enqueue_import_shipped_orders(store, start, end)
            try:
                queue._process_queue()
            except Exception:
                pass
            store.write({"last_shipped_orders_import_time": now})

    # Product import configuration
    auto_create_product_if_not_found = fields.Boolean(
        string="Auto Create Products",
        help="If enabled, products will be created in Odoo when no product with the matching SKU (internal reference) is found.",
    )
    sync_product_images = fields.Boolean(
        string="Sync Product Images",
        default=True,
        help="If enabled, Shopify product images will be downloaded and stored on the Odoo product.",
    )
    import_sales_description = fields.Boolean(
        string="Import Sales Description",
        help="If enabled, Shopify product description (body_html) will be stored on the Odoo product as sales description.",
    )
    use_odoo_description_on_export = fields.Boolean(
        string="Use Odoo Description on Export",
        help="If enabled, when exporting products to Shopify, Odoo product descriptions will overwrite Shopify descriptions.",
    )
    pricelist_id = fields.Many2one(
        "product.pricelist",
        string="Product Pricelist",
        help="Legacy field used by existing export flows. Prefer Shopify Pricelist for new price sync.",
    )
    shopify_pricelist_id = fields.Many2one(
        "product.pricelist",
        string="Shopify Pricelist",
        help="Pricelist used to calculate product prices when updating prices to Shopify.",
    )

    # Stock export configuration
    shopify_stock_type = fields.Selection(
        [
            ("free_qty", "Free To Use Quantity"),
            ("forecast_qty", "Forecast Quantity"),
        ],
        string="Shopify Stock Type",
        default="free_qty",
        help=(
            "Controls how stock is computed when exporting inventory to Shopify.\n"
            "- Free To Use Quantity: qty_available - reserved_quantity\n"
            "- Forecast Quantity: qty_available - outgoing_qty + incoming_qty"
        ),
    )
    export_stock_enabled = fields.Boolean(
        string="Enable Automatic Stock Export",
        help="If enabled, stock changes will be exported to Shopify automatically using the configured scheduler.",
    )
    export_stock_interval_number = fields.Integer(
        string="Stock Export Interval",
        default=25,
        help="Interval between automatic stock exports.",
    )
    export_stock_interval_type = fields.Selection(
        [
            ("minutes", "Minutes"),
            ("hours", "Hours"),
        ],
        string="Stock Export Interval Unit",
        default="minutes",
    )
    last_stock_export_time = fields.Datetime(
        string="Last Stock Export Time",
        readonly=True,
        help="Timestamp of the last automatic stock export run.",
    )

    def _get_api_client(self):
        self.ensure_one()
        shop_url = (self.shop_url or "").strip()
        access_token = (self.access_token or "").strip()
        api_key = (self.api_key or "").strip() or None
        api_secret = (self.api_secret or "").strip() or None

        if not shop_url:
            raise UserError(_("Shop URL is required."))
        if not access_token:
            # The connector is designed to use an Admin API access token.
            # Without it most Shopify Admin API endpoints will fail.
            raise UserError(
                _(
                    "Access Token is not set. Please generate an Admin API access token in Shopify "
                    "and configure it on the store."
                )
            )
        return ShopifyAPI(
            shop_url=shop_url,
            access_token=access_token,
            api_key=api_key,
            api_secret=api_secret,
        )

    def action_test_connection(self):
        for store in self:
            api_client = store._get_api_client()
            try:
                api_client.ping()
            except Exception as e:
                self.env["shopify.sync.log.mixin"].create_log(
                    store=store,
                    log_type="connection",
                    message=str(e),
                    payload=False,
                    status="failed",
                )
                raise UserError(_("Connection failed: %s") % e)
            self.env["shopify.sync.log.mixin"].create_log(
                store=store,
                log_type="connection",
                message=_("Connection successful."),
                payload=False,
                status="success",
            )
        return True

    def action_sync_products(self):
        product_sync = self.env["shopify.product.sync"]
        for store in self:
            product_sync.sync_products(store)
        return True

    def action_product_sync_check(self):
        """Open Product Sync Check wizard to compare products in Odoo vs Shopify."""
        self.ensure_one()
        action = self.env["ir.actions.act_window"]._for_xml_id(
            "custom_odoo_shopify_connector.action_shopify_product_sync_check"
        )
        action["context"] = dict(self.env.context, default_store_id=self.id)
        return action

    def action_sync_orders(self):
        order_sync = self.env["shopify.order.sync"]
        for store in self:
            order_sync.sync_orders(store)
        return True

    def action_sync_customers(self):
        customer_sync = self.env["shopify.customer.sync"]
        for store in self:
            customer_sync.sync_customers(store)
        return True

    def cron_shopify_export_stock(self):
        """Cron entry point: export stock changes to Shopify per active store."""
        from datetime import timedelta

        now = fields.Datetime.now()
        stores = self.search(
            [("active", "=", True), ("export_stock_enabled", "=", True)]
        )
        if not stores:
            return

        stock_service = self.env["shopify.stock.service"]

        for store in stores:
            num = store.export_stock_interval_number or 0
            if num <= 0:
                continue
            itype = store.export_stock_interval_type or "minutes"
            interval_delta = (
                timedelta(minutes=num) if itype == "minutes" else timedelta(hours=num)
            )
            if interval_delta.total_seconds() <= 0:
                continue

            last_run = store.last_stock_export_time or fields.Datetime.from_string(
                "1970-01-01 00:00:00"
            )
            if (now - last_run) < interval_delta:
                continue

            try:
                stock_service.export_stock(
                    store=store,
                    export_from=store.last_stock_export_time,
                    products=None,
                    operation_type="scheduler",
                )
            except Exception:
                # Errors are logged by the stock service; do not crash cron.
                pass

            store.last_stock_export_time = now


