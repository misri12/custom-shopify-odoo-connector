import base64
import json

from odoo import _, api, fields, models


class ShopifyImageUpdateQueue(models.Model):
    _name = "shopify.image.update.queue"
    _description = "Shopify Image Update Queue"
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
            ("manual", "Manual"),
            ("scheduler", "Scheduler"),
        ],
        string="Operation Type",
        required=True,
        default="manual",
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
    products_to_process = fields.Integer(string="Products To Process")
    processed_products = fields.Integer(string="Processed Products")
    error_message = fields.Text(string="Error Message")

    line_ids = fields.One2many(
        "shopify.image.update.queue.line",
        "queue_id",
        string="Image Lines",
        copy=False,
    )

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get("name", _("New")) == _("New"):
                vals["name"] = self.env["ir.sequence"].next_by_code(
                    "shopify.image.update.queue"
                ) or _("New")
        return super().create(vals_list)

    def _process_queue(self, batch_size=50):
        """Process pending image lines in batches."""
        service = self.env["shopify.image.service"]
        for queue in self:
            service.process_queue(queue, batch_size=batch_size)

    @api.model
    def cron_process_pending_queues(self, batch_size=50):
        """Cron entry point: process all pending image update queues in small batches."""
        queues = self.search([("status", "in", ["pending", "processing"])], limit=20)
        for queue in queues:
            queue._process_queue(batch_size=batch_size)


class ShopifyImageUpdateQueueLine(models.Model):
    _name = "shopify.image.update.queue.line"
    _description = "Shopify Image Update Queue Line"

    queue_id = fields.Many2one(
        "shopify.image.update.queue",
        string="Queue",
        required=True,
        ondelete="cascade",
    )
    product_id = fields.Many2one(
        "product.product",
        string="Product",
        required=True,
        ondelete="cascade",
    )
    state = fields.Selection(
        [
            ("pending", "Pending"),
            ("done", "Done"),
            ("failed", "Failed"),
            ("skipped", "Skipped"),
        ],
        string="Status",
        default="pending",
        required=True,
    )
    message = fields.Text(string="Message")


class ShopifyImageService(models.AbstractModel):
    _name = "shopify.image.service"
    _description = "Shopify Image Service"

    def _get_shopify_product_id(self, store, product):
        """Resolve Shopify product id for the given store and product."""
        self.ensure_one()
        # Prefer store-specific product layer mapping
        layer = self.env["shopify.product.layer"].search(
            [
                ("store_id", "=", store.id),
                ("product_tmpl_id", "=", product.product_tmpl_id.id),
                ("shopify_product_id", "!=", False),
            ],
            limit=1,
        )
        if layer and layer.shopify_product_id:
            return layer.shopify_product_id

        # Fallback to product-level field
        if getattr(product, "shopify_product_id", False):
            return product.shopify_product_id

        # Fallback to template-level field
        tmpl = product.product_tmpl_id
        if getattr(tmpl, "shopify_product_id", False):
            return tmpl.shopify_product_id

        return False

    def _get_product_image_base64(self, product):
        """Return base64-encoded image data for the product's template main image."""
        tmpl = product.product_tmpl_id
        image = tmpl.image_1920
        if not image:
            return False

        # image_1920 is already base64-encoded in Odoo. Ensure we return a str.
        if isinstance(image, bytes):
            return base64.b64encode(image).decode("utf-8")
        return image

    def process_queue(self, queue, batch_size=50):
        """Process a single image update queue with logging and rate-limit aware API usage."""
        Log = self.env["shopify.sync.log"].sudo()
        store = queue.store_id
        api_client = store._get_api_client()

        queue.status = "processing"
        pending_lines = queue.line_ids.filtered(lambda l: l.state == "pending")[:batch_size]

        for line in pending_lines:
            product = line.product_id

            shopify_product_id = self._get_shopify_product_id(store, product)
            if not shopify_product_id:
                msg = _(
                    "Missing Shopify product ID mapping for product %s (store %s)."
                ) % (product.display_name, store.name)
                line.state = "failed"
                line.message = msg
                Log.create(
                    {
                        "store_id": store.id,
                        "product_id": product.id,
                        "operation": "product",
                        "message": msg,
                        "status": "failed",
                    }
                )
                continue

            image_b64 = self._get_product_image_base64(product)
            if not image_b64:
                msg = _(
                    "No main image (image_1920) found on product template for %s."
                ) % product.display_name
                line.state = "failed"
                line.message = msg
                Log.create(
                    {
                        "store_id": store.id,
                        "product_id": product.id,
                        "operation": "product",
                        "message": msg,
                        "status": "failed",
                    }
                )
                continue

            payload = {
                "image": {
                    "attachment": image_b64,
                }
            }

            try:
                # Optional: delete existing images before uploading new one
                try:
                    existing = api_client.get_product_images(shopify_product_id)
                    for img in existing or []:
                        img_id = img.get("id")
                        if img_id:
                            api_client.delete_product_image(shopify_product_id, img_id)
                except Exception as delete_err:
                    # Log but do not stop the main image update if delete fails.
                    Log.create(
                        {
                            "store_id": store.id,
                            "product_id": product.id,
                            "operation": "product",
                            "message": _(
                                "Failed to delete existing images on Shopify for %s: %s"
                            )
                            % (product.display_name, str(delete_err)),
                            "status": "failed",
                        }
                    )

                # Upload new main image
                response = api_client.create_product_image(shopify_product_id, payload)
                line.state = "done"
                line.message = _(
                    "Updated main image on Shopify for %s."
                ) % product.display_name

                Log.create(
                    {
                        "store_id": store.id,
                        "product_id": product.id,
                        "operation": "product",
                        "message": line.message,
                        "payload": json.dumps(payload),
                        "response": json.dumps(response or {}),
                        "status": "success",
                    }
                )
            except Exception as e:
                line.state = "failed"
                line.message = str(e)
                queue.error_message = (queue.error_message or "") + "\n" + str(e)
                Log.create(
                    {
                        "store_id": store.id,
                        "product_id": product.id,
                        "operation": "product",
                        "message": "Image update failed for %s: %s"
                        % (product.display_name, str(e)),
                        "status": "failed",
                    }
                )

        queue.processed_products = len(
            queue.line_ids.filtered(lambda l: l.state in ("done", "failed", "skipped"))
        )

        if all(l.state in ("done", "skipped") for l in queue.line_ids):
            queue.status = "done"
        elif any(l.state == "failed" for l in queue.line_ids):
            queue.status = "failed"
        else:
            queue.status = "processing"

