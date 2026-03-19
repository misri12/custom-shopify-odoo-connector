import json

from odoo import api, fields, models, _
from ..services.order_service import OrderService
from ..services.order_import_service import OrderImportService
from ..services.fulfillment_service import ShopifyFulfillmentService


class ShopifyOrderQueue(models.Model):
    _name = "shopify.order.queue"
    _description = "Shopify Order Queue"
    _order = "create_date desc"

    store_id = fields.Many2one(
        "shopify.store",
        required=True,
        ondelete="cascade",
        index=True,
    )
    shopify_order_id = fields.Char(string="Shopify Order ID", index=True)
    payload = fields.Text()
    state = fields.Selection(
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
    log_message = fields.Text()
    job_type = fields.Selection(
        [("order", "Order")],
        string="Job Type",
        default="order",
        required=True,
    )
    retry_count = fields.Integer(default=0, index=True)
    error_message = fields.Text()

    @api.model
    def process_queue(self, limit=50):
        pending_queues = self.search(
            [("state", "=", "pending")], order="create_date asc", limit=limit
        )
        import_service = OrderImportService(self.env)
        order_service = OrderService(self.env, import_service=import_service)
        fulfillment_service = ShopifyFulfillmentService(self.env)
        processed = 0
        max_retries = 3

        for queue in pending_queues:
            try:
                queue.state = "processing"
                payload = {}
                if queue.payload:
                    try:
                        payload = json.loads(queue.payload)
                    except Exception:
                        payload = {}

                workflow = import_service.get_financial_workflow(
                    queue.store_id, payload or {}
                )
                if not workflow:
                    # Skip orders that do not match any financial status rule
                    import_service._log(
                        queue.store_id,
                        _(
                            "Shopify order queue item skipped because no matching "
                            "payment gateway / financial status rule was found."
                        ),
                        payload or queue.payload,
                        status="success",
                    )
                    queue.write(
                        {
                            "state": "done",
                            "log_message": _(
                                "Skipped: no matching payment gateway / financial status rule."
                            ),
                        }
                    )
                    continue

                sale_order = order_service.create_order_from_payload(
                    payload, queue.store_id
                )
                import_service.apply_workflow(
                    sale_order, queue.store_id, payload=payload, workflow=workflow
                )
                fulfillment_service.handle_fulfillment(
                    sale_order, queue.store_id, payload or {}
                )
                queue.write(
                    {
                        "state": "done",
                        "log_message": _("Processed successfully"),
                        "error_message": False,
                    }
                )
                processed += 1
                if processed and processed % 20 == 0:
                    self.env.cr.commit()
            except Exception as e:
                values = {
                    "log_message": str(e),
                    "error_message": str(e),
                }
                if queue.retry_count < max_retries:
                    values.update(
                        {
                            "state": "pending",
                            "retry_count": queue.retry_count + 1,
                        }
                    )
                else:
                    values.update({"state": "failed"})
                queue.write(values)
                self.env["shopify.sync.log.mixin"].create_log(
                    store=queue.store_id,
                    log_type="order",
                    message=str(e),
                    payload=queue.payload,
                    status="failed",
                )
                processed += 1
                if processed and processed % 20 == 0:
                    self.env.cr.commit()

    @api.model
    def cron_process_order_queue(self):
        self.process_queue()

