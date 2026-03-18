from odoo import _, api, fields, models

from ..services.queue_service import ProductQueueService


class ShopifyProductQueue(models.Model):
    _name = "shopify.product.queue"
    _description = "Shopify Product Queue (Batch)"
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
    do_not_update_existing = fields.Boolean(
        string="Do Not Update Existing Products",
        default=False,
    )
    state = fields.Selection(
        [
            ("draft", "Draft"),
            ("partial", "Partially Completed"),
            ("done", "Completed"),
            ("failed", "Failed"),
        ],
        string="State",
        default="draft",
        required=True,
        index=True,
        compute="_compute_state",
        store=True,
        readonly=True,
    )
    line_ids = fields.One2many(
        "shopify.product.queue.line",
        "queue_id",
        string="Product Lines",
        copy=False,
    )
    total_records = fields.Integer(
        string="Total Records",
        compute="_compute_record_counts",
        store=True,
    )
    done_records = fields.Integer(
        string="Done",
        compute="_compute_record_counts",
        store=True,
    )
    failed_records = fields.Integer(
        string="Failed",
        compute="_compute_record_counts",
        store=True,
    )
    draft_records = fields.Integer(
        string="Draft",
        compute="_compute_record_counts",
        store=True,
    )

    @api.depends("line_ids.state")
    def _compute_record_counts(self):
        for rec in self:
            lines = rec.line_ids
            rec.total_records = len(lines)
            rec.done_records = len(lines.filtered(lambda l: l.state == "done"))
            rec.failed_records = len(lines.filtered(lambda l: l.state == "failed"))
            rec.draft_records = len(lines.filtered(lambda l: l.state == "draft"))

    @api.depends("line_ids.state")
    def _compute_state(self):
        for rec in self:
            total = len(rec.line_ids)
            if total == 0:
                rec.state = "draft"
            elif rec.draft_records == total:
                rec.state = "draft"
            elif rec.draft_records > 0:
                rec.state = "partial"
            elif rec.failed_records == total:
                rec.state = "failed"
            else:
                rec.state = "done"

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get("name", _("New")) == _("New"):
                vals["name"] = self.env["ir.sequence"].next_by_code(
                    "shopify.product.queue"
                ) or _("New")
        return super().create(vals_list)

    def action_process_manually(self):
        """Process draft lines of selected queue(s)."""
        service = ProductQueueService(self.env)
        for queue in self:
            service.process_queue(queue)
        return True

    def action_set_to_completed(self):
        """Mark all draft lines as done and set queue to completed."""
        for queue in self:
            queue.line_ids.filtered(lambda l: l.state == "draft").write(
                {"state": "done", "message": "Manually set to completed"}
            )
        return True

    def action_fetch_products(self):
        """Fetch products from Shopify and add them as lines to this queue."""
        service = ProductQueueService(self.env)
        for queue in self:
            service.fetch_products_into_queue(queue)
        return True

    @api.model
    def process_draft_queues_cron(self):
        """Cron: process all queues that have draft lines."""
        service = ProductQueueService(self.env)
        return service.process_draft_queues()
