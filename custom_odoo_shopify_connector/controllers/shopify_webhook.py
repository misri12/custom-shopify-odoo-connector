import base64
import hashlib
import hmac
import json

from odoo import http
from odoo.http import request


class ShopifyCancellationWebhookController(http.Controller):
    @http.route(
        "/shopify/webhook/order_cancelled",
        type="http",
        auth="public",
        methods=["POST"],
        csrf=False,
    )
    def shopify_order_cancelled_webhook(self, **kwargs):
        raw_data = request.httprequest.get_data()
        shop_domain = request.httprequest.headers.get("X-Shopify-Shop-Domain")
        hmac_header = request.httprequest.headers.get("X-Shopify-Hmac-Sha256")

        try:
            payload = json.loads(raw_data.decode("utf-8") or "{}")
        except Exception:
            request.env["shopify.sync.log.mixin"].create_log(
                store=False,
                log_type="order",
                message="Shopify orders/cancelled webhook: invalid JSON payload.",
                payload=raw_data.decode("utf-8", errors="ignore"),
                status="failed",
            )
            return request.make_response("Invalid JSON", status=400)

        store = (
            request.env["shopify.store"]
            .sudo()
            .search([("shop_url", "ilike", shop_domain)], limit=1)
        )
        if not store:
            request.env["shopify.sync.log.mixin"].create_log(
                store=False,
                log_type="order",
                message="Shopify orders/cancelled webhook: unknown store %s" % (shop_domain or ""),
                payload=raw_data.decode("utf-8", errors="ignore"),
                status="failed",
            )
            return request.make_response("Unknown store", status=404)

        if store.webhook_secret:
            if not self._verify_hmac(store.webhook_secret, raw_data, hmac_header):
                request.env["shopify.sync.log.mixin"].create_log(
                    store=store,
                    log_type="order",
                    message="Shopify orders/cancelled webhook: invalid HMAC signature.",
                    payload=raw_data.decode("utf-8", errors="ignore"),
                    status="failed",
                )
                return request.make_response("Invalid webhook signature", status=403)

        shopify_order_id = str(payload.get("id") or payload.get("order_id") or "")
        cancel_reason = payload.get("cancel_reason") or payload.get("reason") or ""

        request.env["shopify.sync.log.mixin"].create_log(
            store=store,
            log_type="order",
            message="Shopify webhook received: orders/cancelled",
            payload=raw_data.decode("utf-8", errors="ignore")[:5000],
            status="success",
            order_id=shopify_order_id or False,
        )

        if not shopify_order_id:
            return request.make_response(json.dumps({"success": True}), status=200)

        SaleOrder = request.env["sale.order"].sudo()
        order = SaleOrder.search(
            [
                ("shopify_order_id", "=", shopify_order_id),
                ("shopify_instance_id", "=", store.id),
            ],
            limit=1,
        )
        if not order:
            return request.make_response(json.dumps({"success": True}), status=200)

        if order.state == "cancel" or order.shopify_cancelled:
            return request.make_response(json.dumps({"success": True}), status=200)

        try:
            order.action_cancel()
        except Exception:
            order.write({"state": "cancel"})

        order.write(
            {
                "shopify_cancelled": True,
                "shopify_cancel_reason": cancel_reason or "shopify",
            }
        )

        request.env["shopify.sync.log.mixin"].create_log(
            store=store,
            log_type="order",
            message="Odoo sale order cancelled from Shopify webhook.",
            payload=json.dumps({"odoo_order": order.name}),
            status="success",
            order_id=shopify_order_id,
        )

        return request.make_response(json.dumps({"success": True}), status=200)

    def _verify_hmac(self, secret, data, hmac_header):
        if not hmac_header:
            return False
        digest = hmac.new(secret.encode("utf-8"), data, hashlib.sha256).digest()
        calculated = base64.b64encode(digest).decode("utf-8")
        return hmac.compare_digest(calculated, hmac_header)


class ShopifyRefundWebhookController(http.Controller):
    @http.route(
        "/shopify/webhook/refund_created",
        type="http",
        auth="public",
        methods=["POST"],
        csrf=False,
    )
    def shopify_refund_created_webhook(self, **kwargs):
        raw_data = request.httprequest.get_data()
        shop_domain = request.httprequest.headers.get("X-Shopify-Shop-Domain")
        hmac_header = request.httprequest.headers.get("X-Shopify-Hmac-Sha256")

        try:
            payload = json.loads(raw_data.decode("utf-8") or "{}")
        except Exception:
            request.env["shopify.sync.log.mixin"].create_log(
                store=False,
                log_type="order",
                message="Shopify refunds/create webhook: invalid JSON payload.",
                payload=raw_data.decode("utf-8", errors="ignore"),
                status="failed",
            )
            return request.make_response("Invalid JSON", status=400)

        store = (
            request.env["shopify.store"]
            .sudo()
            .search([("shop_url", "ilike", shop_domain)], limit=1)
        )
        if not store:
            request.env["shopify.sync.log.mixin"].create_log(
                store=False,
                log_type="order",
                message="Shopify refunds/create webhook: unknown store %s" % (shop_domain or ""),
                payload=raw_data.decode("utf-8", errors="ignore"),
                status="failed",
            )
            return request.make_response("Unknown store", status=404)

        if store.webhook_secret:
            digest = hmac.new(store.webhook_secret.encode("utf-8"), raw_data, hashlib.sha256).digest()
            calculated = base64.b64encode(digest).decode("utf-8")
            if not hmac.compare_digest(calculated, hmac_header or ""):
                request.env["shopify.sync.log.mixin"].create_log(
                    store=store,
                    log_type="order",
                    message="Shopify refunds/create webhook: invalid HMAC signature.",
                    payload=raw_data.decode("utf-8", errors="ignore"),
                    status="failed",
                )
                return request.make_response("Invalid webhook signature", status=403)

        shopify_order_id = str(payload.get("order_id") or payload.get("order", {}).get("id") or "")
        refund_id = str(payload.get("id") or "")

        request.env["shopify.sync.log.mixin"].create_log(
            store=store,
            log_type="order",
            message="Shopify webhook received: refunds/create",
            payload=raw_data.decode("utf-8", errors="ignore")[:5000],
            status="success",
            order_id=shopify_order_id or False,
        )

        if not shopify_order_id:
            return request.make_response(json.dumps({"success": True}), status=200)

        SaleOrder = request.env["sale.order"].sudo()
        order = SaleOrder.search(
            [
                ("shopify_order_id", "=", shopify_order_id),
                ("shopify_instance_id", "=", store.id),
            ],
            limit=1,
        )
        if not order:
            return request.make_response(json.dumps({"success": True}), status=200)

        # Duplicate refund protection by Shopify refund id
        Move = request.env["account.move"].sudo()
        if refund_id and Move.search_count([("shopify_refund_id", "=", refund_id)]):
            return request.make_response(json.dumps({"success": True}), status=200)

        # Find a posted invoice to reverse
        invoice = Move.search(
            [
                ("move_type", "=", "out_invoice"),
                ("state", "=", "posted"),
                ("invoice_origin", "=", order.name),
            ],
            limit=1,
        )
        if not invoice:
            return request.make_response(json.dumps({"success": True}), status=200)

        # Create reversal credit note
        credit = invoice._reverse_moves(
            default_values_list=[{"ref": _("Shopify refund %s") % (refund_id or shopify_order_id)}]
        )
        if credit:
            credit.action_post()
            # Mark as synced and store Shopify refund id
            credit.write(
                {
                    "shopify_refunded": True,
                    "shopify_refund_date": fields.Datetime.now(),
                    "shopify_refund_id": refund_id or False,
                }
            )
            order.write({"shopify_refunded": True, "shopify_refund_date": fields.Datetime.now()})

        return request.make_response(json.dumps({"success": True}), status=200)
