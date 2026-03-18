import base64
import hashlib
import hmac
import json

from odoo import http
from odoo.http import request


class ShopifyWebhookController(http.Controller):
    @http.route(
        "/shopify/webhook/order",
        type="http",
        auth="public",
        methods=["POST"],
        csrf=False,
    )
    # v44m-a3hs-qc6u
    def shopify_order_webhook(self, **kwargs):
        raw_data = request.httprequest.get_data()
        shop_domain = request.httprequest.headers.get("X-Shopify-Shop-Domain")
        hmac_header = request.httprequest.headers.get("X-Shopify-Hmac-Sha256")

        try:
            payload = json.loads(raw_data.decode("utf-8") or "{}")
        except Exception:
            request.env["shopify.sync.log.mixin"].create_log(
                store=False,
                log_type="order",
                message="Shopify webhook: invalid JSON payload.",
                payload=raw_data.decode("utf-8", errors="ignore"),
                status="failed",
            )
            return request.make_response(
                "Invalid JSON", status=400, headers=[("Content-Type", "text/plain")]
            )

        store = (
            request.env["shopify.store"]
            .sudo()
            .search([("shop_url", "ilike", shop_domain)], limit=1)
        )
        if not store:
            request.env["shopify.sync.log.mixin"].create_log(
                store=False,
                log_type="order",
                message="Shopify webhook: unknown store %s" % (shop_domain or ""),
                payload=raw_data.decode("utf-8", errors="ignore"),
                status="failed",
            )
            return request.make_response(
                "Unknown store", status=404, headers=[("Content-Type", "text/plain")]
            )

        if store.webhook_secret:
            if not self._verify_hmac(store.webhook_secret, raw_data, hmac_header):
                request.env["shopify.sync.log.mixin"].create_log(
                    store=store,
                    log_type="order",
                    message="Shopify webhook: invalid HMAC signature.",
                    payload=raw_data.decode("utf-8", errors="ignore"),
                    status="failed",
                )
                return request.make_response(
                    "Invalid webhook signature",
                    status=403,
                    headers=[("Content-Type", "text/plain")],
                )

        handler = request.env["shopify.webhook.handler"].sudo()
        handler.process_webhook_order(payload, store)

        return request.make_response(
            json.dumps({"success": True}),
            status=200,
            headers=[("Content-Type", "application/json")],
        )

    @http.route(
        "/shopify/webhook/order_create",
        type="jsonrpc",
        auth="public",
        methods=["POST"],
        csrf=False,
    )
    def shopify_order_create_webhook(self, **kwargs):
        """Alternate endpoint for orders/create and orders/updated webhooks."""
        topic = request.httprequest.headers.get("X-Shopify-Topic") or ""
        if topic not in ("orders/create", "orders/updated"):
            return {"success": False, "error": "Unsupported topic"}
        return self.shopify_order_webhook(**kwargs)

    def _verify_hmac(self, secret, data, hmac_header):
        if not hmac_header:
            return False
        digest = hmac.new(
            secret.encode("utf-8"),
            data,
            hashlib.sha256,
        ).digest()
        calculated = base64.b64encode(digest).decode("utf-8")
        return hmac.compare_digest(calculated, hmac_header)

