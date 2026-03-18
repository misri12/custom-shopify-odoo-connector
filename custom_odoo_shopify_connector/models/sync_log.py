from odoo import api, fields, models


class ShopifySyncLog(models.Model):
    _name = "shopify.sync.log"
    _description = "Shopify Synchronization Log"
    _order = "create_date desc"

    name = fields.Char(string="Name", default=lambda self: _("Shopify Sync Log"))
    shopify_id = fields.Char(string="Shopify Resource ID", index=True)
    store_id = fields.Many2one("shopify.store", string="Store")
    order_id = fields.Char(
        string="Shopify Order ID",
        index=True,
        help="Shopify order id related to this log entry (when applicable).",
    )
    product_id = fields.Many2one(
        "product.product",
        string="Product",
        help="Odoo product related to this log entry (when applicable).",
    )
    instance_id = fields.Many2one(
        "shopify.store",
        string="Shopify Instance",
        related="store_id",
        store=True,
        readonly=True,
    )
    operation = fields.Selection(
        [
            ("connection", "Connection"),
            ("product", "Product"),
            ("customer", "Customer"),
            ("order", "Order"),
            ("inventory", "Inventory"),
            ("shipping", "Shipping"),
            ("fulfillment", "Fulfillment"),
            ("other", "Other"),
        ],
        string="Operation",
        required=True,
        default="other",
    )
    message = fields.Text(required=True)
    payload = fields.Text()
    response = fields.Text(string="API Response", help="Raw API response or error details.")
    status = fields.Selection(
        [("success", "Success"), ("failed", "Failed")],
        required=True,
        default="success",
    )
    timestamp = fields.Datetime(
        string="Timestamp",
        default=lambda self: fields.Datetime.now(),
        required=True,
        index=True,
        help="When this log entry was created (functional timestamp for reporting).",
    )
    # Backward compatibility: keep existing column used by older data / views.
    date = fields.Datetime(
        string="Date",
        default=lambda self: fields.Datetime.now(),
        required=True,
        help="Deprecated: use Timestamp instead.",
    )


class ShopifySyncLogMixin(models.AbstractModel):
    _name = "shopify.sync.log.mixin"
    _description = "Shopify Sync Log Mixin"

    @api.model
    def create_log(
        self, store, log_type, message, payload=None, status="success", response=None, order_id=None
    ):
        # Always use sudo to ensure that technical logging never fails with
        # AccessError for regular users or background jobs.
        return self.env["shopify.sync.log"].sudo().create(
            {
                "store_id": store.id if store else False,
                "order_id": str(order_id) if order_id else False,
                "operation": log_type,
                "message": message,
                "payload": payload or "",
                "response": response or "",
                "status": status,
            }
        )

