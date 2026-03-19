from odoo import _, api, fields, models


class ShopifyCustomerImportQueue(models.Model):
    _name = "shopify.customer.import.queue"
    _description = "Shopify Customer Import Queue"
    _order = "create_date desc"

    name = fields.Char(
        string="Queue Reference",
        required=True,
        copy=False,
        readonly=True,
        default=lambda self: _("New"),
    )
    store_id = fields.Many2one(
        "shopify.store",
        string="Instance",
        required=True,
        ondelete="cascade",
    )
    operation_type = fields.Selection(
        [
            ("import_customers", "Import Customers"),
        ],
        string="Operation Type",
        default="import_customers",
        required=True,
    )
    status = fields.Selection(
        [
            ("pending", "Pending"),
            ("processing", "Processing"),
            ("done", "Done"),
            ("failed", "Failed"),
        ],
        string="Status",
        default="pending",
        required=True,
    )
    records_to_process = fields.Integer(string="Records To Process")
    processed_records = fields.Integer(string="Processed Records")
    error_message = fields.Text(string="Error Message")

    def action_process_manually(self):
        """Button: Process Queue Manually."""
        for queue in self:
            self.env["shopify.service"].import_customers_from_queue(queue)
        return True

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get("name", _("New")) == _("New"):
                vals["name"] = self.env["ir.sequence"].next_by_code(
                    "shopify.customer.import.queue"
                ) or _("New")
        return super().create(vals_list)

    @api.model
    def cron_process_pending_queues(self):
        """Cron: automatically process pending customer import queues."""
        pending = self.search([("status", "=", "pending")], order="create_date asc", limit=5)
        for queue in pending:
            self.env["shopify.service"].import_customers_from_queue(queue)

