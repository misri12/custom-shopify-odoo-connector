from odoo import _


class ShopifyFulfillmentService:
    def __init__(self, env):
        self.env = env

    def _get_relevant_pickings(self, order):
        """Return non-done, non-cancelled pickings for the sale order."""
        return order.picking_ids.filtered(lambda p: p.state not in ("done", "cancel"))

    def _apply_full_fulfillment(self, pickings):
        for picking in pickings:
            if picking.state not in ("assigned", "confirmed"):
                picking.action_assign()
            for move_line in picking.move_line_ids:
                if move_line.product_uom_qty and not move_line.qty_done:
                    move_line.qty_done = move_line.product_uom_qty
            picking.button_validate()

    def _apply_partial_fulfillment(self, order, pickings, payload):
        """Deliver only fulfilled quantities based on Shopify fulfillments."""
        fulfillments = payload.get("fulfillments") or []
        if not fulfillments:
            return

        # Map (variant_id, sku) to fulfilled quantity
        fulfilled_qty_map = {}
        for fulfillment in fulfillments:
            for line in fulfillment.get("line_items") or []:
                key = (
                    str(line.get("variant_id") or "") or None,
                    line.get("sku") or None,
                )
                try:
                    qty = float(line.get("quantity") or 0.0)
                except Exception:
                    qty = 0.0
                if key not in fulfilled_qty_map:
                    fulfilled_qty_map[key] = 0.0
                fulfilled_qty_map[key] += qty

        if not fulfilled_qty_map:
            return

        # Map sale order lines to fulfilled quantities
        for line in order.order_line:
            variant_id = getattr(line.product_id, "shopify_variant_id", False)
            key = (str(variant_id) if variant_id else None, line.product_id.default_code)
            fulfilled_qty = fulfilled_qty_map.get(key)
            if not fulfilled_qty:
                continue

            remaining = fulfilled_qty
            for move in pickings.move_ids.filtered(
                lambda m: m.product_id == line.product_id
            ):
                for move_line in move.move_line_ids:
                    if remaining <= 0:
                        break
                    qty_available = move_line.product_uom_qty - move_line.qty_done
                    if qty_available <= 0:
                        continue
                    to_set = min(qty_available, remaining)
                    move_line.qty_done += to_set
                    remaining -= to_set

        for picking in pickings:
            if any(
                line.qty_done > 0.0
                for line in picking.move_line_ids
                if line.state not in ("done", "cancel")
            ):
                if picking.state not in ("assigned", "confirmed"):
                    picking.action_assign()
                picking.button_validate()

    def handle_fulfillment(self, order, store, payload):
        """Apply fulfillment behavior based on Shopify fulfillment status.

        - fulfilled: create/ensure deliveries and validate them.
        - partial: deliver only fulfilled quantities.
        - null: keep pickings open but do not validate.
        """
        if not order or not store:
            return

        fulfillment_status = (payload or {}).get("fulfillment_status")

        pickings = self._get_relevant_pickings(order)
        if not pickings:
            # Rely on standard stock rules triggered by the sale order workflow.
            return

        if fulfillment_status == "fulfilled":
            self._apply_full_fulfillment(pickings)
        elif fulfillment_status == "partial":
            self._apply_partial_fulfillment(order, pickings, payload or {})
        else:
            # Do not validate pickings when fulfillment_status is null or unrecognized.
            return

