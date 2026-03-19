import json

from odoo import api, models


class ShopifyWebhookHandler(models.AbstractModel):
    _name = "shopify.webhook.handler"
    _description = "Shopify Webhook Handler"

    @api.model
    def process_webhook_order(self, payload, store):
        if not store:
            return False

        # Allow disabling webhook-based order handling per store
        if not store.manage_orders_webhook:
            return False

        shopify_order_id = payload.get("id") or payload.get("order_id")

        queue_model = self.env["shopify.order.queue"]

        # Avoid creating multiple pending/processing queue records
        if shopify_order_id:
            existing = queue_model.search(
                [
                    ("store_id", "=", store.id),
                    ("shopify_order_id", "=", str(shopify_order_id)),
                    ("state", "in", ["pending", "processing"]),
                ],
                limit=1,
            )
            if existing:
                return True

        queue_model.create(
            {
                "store_id": store.id,
                "shopify_order_id": str(shopify_order_id) if shopify_order_id else False,
                "payload": json.dumps(payload),
                "state": "pending",
            }
        )
        return True

