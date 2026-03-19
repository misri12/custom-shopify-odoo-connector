import json
from datetime import timedelta

from odoo import api, fields, models, _
from odoo.exceptions import UserError


class ShopifyImportQueue(models.Model):
    _name = "shopify.import.queue"
    _description = "Shopify Import Queue"
    _order = "create_date desc, id desc"

    store_id = fields.Many2one("shopify.store", string="Instance", required=True, index=True, ondelete="cascade")
    operation_type = fields.Selection(
        [
            ("import_shipped_orders", "Import Shipped Orders"),
        ],
        required=True,
        index=True,
    )
    status = fields.Selection(
        [
            ("pending", "Pending"),
            ("processing", "Processing"),
            ("done", "Done"),
            ("failed", "Failed"),
        ],
        default="pending",
        required=True,
        index=True,
    )
    start_date = fields.Datetime(required=True, index=True)
    end_date = fields.Datetime(required=True, index=True)
    records_to_process = fields.Integer(default=0)
    processed_records = fields.Integer(default=0)
    error_message = fields.Text()

    def action_process_queue_manually(self):
        for queue in self:
            queue._process_queue()
        return True

    def _process_queue(self):
        self.ensure_one()
        if self.status in ("processing", "done"):
            return

        self.status = "processing"
        self.error_message = False
        self.processed_records = 0

        try:
            if self.operation_type == "import_shipped_orders":
                self.env["shopify.service"].import_shipped_orders_from_queue(self)
            else:
                raise UserError(_("Unsupported queue operation type: %s") % self.operation_type)
            self.status = "done"
        except Exception as e:
            self.status = "failed"
            self.error_message = str(e)
            self.env["shopify.sync.log.mixin"].create_log(
                store=self.store_id,
                log_type="order",
                message=_("Queue processing failed: %s") % str(e),
                payload=json.dumps(
                    {
                        "queue_id": self.id,
                        "operation_type": self.operation_type,
                        "start_date": fields.Datetime.to_string(self.start_date),
                        "end_date": fields.Datetime.to_string(self.end_date),
                    }
                ),
                status="failed",
            )

