import json

from odoo import api, fields, models, _
from odoo.exceptions import UserError

from ..services.queue_service import _shopify_datetime


class ShopifyService(models.AbstractModel):
    _name = "shopify.service"
    _description = "Shopify Service"

    @api.model
    def _log(self, store, message, payload=None, status="success", order_id=None, operation="order", response=None):
        return self.env["shopify.sync.log.mixin"].create_log(
            store=store,
            log_type=operation,
            message=message,
            payload=payload or "",
            status=status,
            response=response,
            order_id=order_id,
        )

    @api.model
    def enqueue_import_shipped_orders(self, store, start_date, end_date):
        Queue = self.env["shopify.import.queue"]
        queue = Queue.create(
            {
                "store_id": store.id,
                "operation_type": "import_shipped_orders",
                "status": "pending",
                "start_date": start_date,
                "end_date": end_date,
            }
        )
        self._log(
            store,
            _("Shipped orders import queued: %(start)s → %(end)s")
            % {"start": fields.Datetime.to_string(start_date), "end": fields.Datetime.to_string(end_date)},
            payload=json.dumps({"queue_id": queue.id}),
            status="success",
            operation="order",
        )
        return queue

    @api.model
    def enqueue_stock_import(self, store):
        """Create a stock import queue for the given store."""
        Queue = self.env["shopify.stock.import.queue"]
        queue = Queue.create(
            {
                "store_id": store.id,
                "operation_type": "import_stock",
                "status": "pending",
            }
        )
        self._log(
            store,
            _("Stock import queued."),
            payload=json.dumps({"queue_id": queue.id}),
            status="success",
            operation="inventory",
        )
        return queue

    @api.model
    def enqueue_customer_import(self, store):
        """Create a customer import queue for the given store."""
        Queue = self.env["shopify.customer.import.queue"]
        queue = Queue.create(
            {
                "store_id": store.id,
                "operation_type": "import_customers",
                "status": "pending",
            }
        )
        self._log(
            store,
            _("Customer import queued."),
            payload=json.dumps({"queue_id": queue.id}),
            status="success",
            operation="customer",
        )
        return queue

    @api.model
    def import_customers_from_queue(self, queue, batch_size=250):
        """Fetch customers from Shopify and create/update partners via the queue."""
        from ..services.shopify_api import ShopifyAPI

        queue.ensure_one()
        store = queue.store_id
        api_client = ShopifyAPI(
            shop_url=store.shop_url,
            access_token=store.access_token,
            api_key=store.api_key,
            api_secret=store.api_secret,
        )

        try:
            customers = api_client.get_customers(limit=batch_size)
        except Exception as e:
            self._log(
                store,
                _("Failed to fetch customers from Shopify: %s") % str(e),
                payload="",
                status="failed",
                operation="customer",
            )
            queue.status = "failed"
            queue.error_message = str(e)
            return

        queue.status = "processing"
        queue.records_to_process = len(customers)

        Partner = self.env["res.partner"].sudo()
        processed = 0

        for c in customers:
            processed += 1
            shopify_id = str(c.get("id") or "")
            email = (c.get("email") or "").strip()
            first_name = c.get("first_name") or ""
            last_name = c.get("last_name") or ""
            phone = c.get("phone") or False
            name = (first_name + " " + last_name).strip() or email or _("Shopify Customer")

            # Duplicate protection: by shopify_customer_id, then email
            partner = False
            if shopify_id:
                partner = Partner.search(
                    [("shopify_customer_id", "=", shopify_id)], limit=1
                )
            if not partner and email:
                partner = Partner.search([("email", "=", email)], limit=1)

            addresses = c.get("addresses") or []
            default_address = c.get("default_address") or (addresses[0] if addresses else None)

            def _map_address_vals(addr):
                if not addr:
                    return {}
                country_code = addr.get("country_code") or addr.get("country_code_v2")
                province_code = addr.get("province_code")
                country = self.env["res.country"].search(
                    [("code", "=", country_code)], limit=1
                )
                state = False
                if country and province_code:
                    state = self.env["res.country.state"].search(
                        [("code", "=", province_code), ("country_id", "=", country.id)],
                        limit=1,
                    )
                return {
                    "street": addr.get("address1"),
                    "street2": addr.get("address2"),
                    "city": addr.get("city"),
                    "zip": addr.get("zip"),
                    "country_id": country.id if country else False,
                    "state_id": state.id if state else False,
                }

            base_vals = {
                "name": name,
                "email": email,
                "phone": phone,
                "shopify_customer_id": shopify_id or False,
                "customer_rank": 1,
            }

            billing_vals = _map_address_vals(default_address)
            if partner:
                partner.write(base_vals | billing_vals)
            else:
                partner = Partner.create(base_vals | billing_vals)

            # Shipping address (child partner)
            shipping_addr = None
            for addr in addresses:
                if default_address and addr.get("id") == default_address.get("id"):
                    continue
                shipping_addr = addr
                break

            if shipping_addr:
                shipping_vals = _map_address_vals(shipping_addr)
                shipping_vals.update(
                    {
                        "parent_id": partner.id,
                        "type": "delivery",
                        "name": partner.name,
                    }
                )
                existing_delivery = Partner.search(
                    [("parent_id", "=", partner.id), ("type", "=", "delivery")],
                    limit=1,
                )
                if existing_delivery:
                    existing_delivery.write(shipping_vals)
                else:
                    Partner.create(shipping_vals)

            queue.processed_records = processed

        queue.status = "done"

        self._log(
            store,
            _("Customers import completed. Records: %s") % queue.records_to_process,
            payload="",
            status="success",
            operation="customer",
        )

    @api.model
    def import_stock_from_queue(self, queue, auto_apply_inventory_adjustment=True):
        """Fetch inventory levels from Shopify and apply to Odoo via the queue."""
        queue.ensure_one()
        store = queue.store_id
        api_client = store._get_api_client()
        VariantMap = self.env["shopify.variant.map"]
        Product = self.env["product.product"]

        params = {}
        if store.shopify_location_id:
            params["location_ids"] = store.shopify_location_id

        try:
            inventory_levels = api_client.get_inventory(**params)
        except Exception as e:
            self._log(
                store,
                _("Failed to fetch inventory from Shopify: %s") % str(e),
                payload=json.dumps(params),
                status="failed",
                operation="inventory",
            )
            queue.status = "failed"
            queue.error_message = str(e)
            return

        queue.records_to_process = len(inventory_levels)

        # Build mapping: inventory_item_id -> variant map
        item_id_to_map = {}
        if inventory_levels:
            item_ids = list({str(lvl.get("inventory_item_id")) for lvl in inventory_levels})
            maps = VariantMap.search(
                [
                    ("store_id", "=", store.id),
                    ("shopify_inventory_item_id", "in", item_ids),
                ]
            )
            for m in maps:
                item_id_to_map[m.shopify_inventory_item_id] = m

        Line = self.env["shopify.stock.import.queue.line"]

        for lvl in inventory_levels:
            inventory_item_id = str(lvl.get("inventory_item_id") or "")
            available = float(lvl.get("available") or 0.0)

            mapping = item_id_to_map.get(inventory_item_id)
            if not mapping:
                self._log(
                    store,
                    _("No product mapping for inventory item %s; skipped.")
                    % inventory_item_id,
                    payload=json.dumps(lvl),
                    status="failed",
                    operation="inventory",
                )
                continue

            product = mapping.product_id
            # Lot/serial products will be skipped later per line
            # Compute current Odoo stock in main warehouse location
            location = store.default_unshipped_order_warehouse_id.lot_stock_id or self.env.ref(
                "stock.stock_location_stock"
            )

            Quant = self.env["stock.quant"].sudo()
            quant = Quant.search(
                [
                    ("product_id", "=", product.id),
                    ("location_id", "=", location.id),
                    ("company_id", "=", store.company_id.id),
                ],
                limit=1,
            )
            odoo_qty = quant.quantity if quant else 0.0
            difference = available - odoo_qty

            Line.create(
                {
                    "queue_id": queue.id,
                    "product_id": product.id,
                    "inventory_item_id": inventory_item_id,
                    "shopify_location_id": str(lvl.get("location_id") or ""),
                    "shopify_available_qty": available,
                    "odoo_qty": odoo_qty,
                    "difference_qty": difference,
                    "state": "pending",
                }
            )

        # Process the queue lines
        queue._process_queue(auto_apply_inventory_adjustment=auto_apply_inventory_adjustment)

        self._log(
            store,
            _("Stock import completed. Records: %s") % queue.records_to_process,
            payload=json.dumps({"queue_id": queue.id}),
            status="success",
            operation="inventory",
        )

    @api.model
    def import_shipped_orders_from_queue(self, queue):
        """Fetch shipped/fulfilled orders from Shopify and import them into Odoo.

        - financial_status: paid OR refunded
        - fulfillment_status: fulfilled
        - duplicate protection via sale.order.shopify_order_id
        - creates sale.order + invoices
        - does NOT create delivery orders; creates inventory adjustment moves instead
        """
        queue.ensure_one()
        store = queue.store_id
        api_client = store._get_api_client()

        params = {
            "status": "any",
            "fulfillment_status": "fulfilled",
            "created_at_min": _shopify_datetime(queue.start_date),
            "created_at_max": _shopify_datetime(queue.end_date),
            "limit": 250,
        }

        self._log(store, _("Shopify API request: fetch fulfilled orders"), payload=json.dumps(params), operation="order")
        orders = api_client.get_orders(**params) or []
        queue.records_to_process = len(orders)

        SaleOrder = self.env["sale.order"].sudo()
        imported = 0
        processed = 0

        for order in orders:
            processed += 1
            shopify_order_id = str(order.get("id") or "")
            queue.processed_records = processed

            if not shopify_order_id:
                continue

            # Filter: paid + fulfilled OR refunded + fulfilled
            financial_status = (order.get("financial_status") or "").lower()
            fulfillment_status = (order.get("fulfillment_status") or "").lower()
            if fulfillment_status != "fulfilled":
                continue
            if financial_status not in ("paid", "refunded", "partially_refunded"):
                continue

            # Duplicate protection
            if SaleOrder.search_count([("shopify_order_id", "=", shopify_order_id)]):
                self._log(
                    store,
                    _("Skipped duplicate Shopify order %s") % shopify_order_id,
                    status="success",
                    order_id=shopify_order_id,
                    operation="order",
                )
                continue

            try:
                self._import_single_fulfilled_order(store, order)
                imported += 1
            except Exception as e:
                self._log(
                    store,
                    _("Failed to import fulfilled order %s: %s") % (shopify_order_id, str(e)),
                    payload=json.dumps(order)[:5000],
                    status="failed",
                    order_id=shopify_order_id,
                    operation="order",
                )

            if processed and processed % 20 == 0:
                self.env.cr.commit()

        self._log(
            store,
            _("Fulfilled orders import completed. Imported: %s") % imported,
            payload=json.dumps({"queue_id": queue.id, "imported": imported, "total": len(orders)}),
            status="success",
            operation="order",
        )

    @api.model
    def _import_single_fulfilled_order(self, store, payload):
        """Create sale.order + invoices and inventory adjustment moves."""
        from ..services.order_service import OrderService
        from ..services.order_import_service import OrderImportService

        shopify_order_id = str(payload.get("id") or "")
        if not shopify_order_id:
            raise UserError(_("Missing Shopify order id in payload."))

        import_service = OrderImportService(self.env)
        order_service = OrderService(self.env, import_service=import_service)

        sale_order = order_service.create_order_from_payload(payload, store)

        # Mark as fulfilled import
        try:
            sale_order.shopify_fulfillment_status = "fulfilled"
        except Exception:
            pass

        # Avoid delivery orders: cancel and remove any generated pickings (if any)
        for picking in sale_order.picking_ids:
            if picking.state not in ("done", "cancel"):
                picking.action_cancel()
            if picking.state == "cancel":
                try:
                    picking.unlink()
                except Exception:
                    pass

        # Invoices: create & post
        invoices = sale_order._create_invoices()
        if invoices:
            invoices.action_post()

        financial_status = (payload.get("financial_status") or "").lower()
        if financial_status in ("refunded", "partially_refunded"):
            # Create refund (credit note) for posted invoices
            for inv in invoices:
                refund = inv._reverse_moves(default_values_list=[{"ref": _("Refund for Shopify order %s") % shopify_order_id}])
                if refund:
                    refund.action_post()

        # Inventory adjustment via stock moves (Stock -> Inventory Loss)
        self._create_inventory_adjustment_moves(store, sale_order, payload)

        self._log(
            store,
            _("Imported fulfilled Shopify order %s") % shopify_order_id,
            payload=json.dumps({"odoo_order": sale_order.name}),
            status="success",
            order_id=shopify_order_id,
            operation="order",
        )
        return sale_order

    @api.model
    def _create_inventory_adjustment_moves(self, store, sale_order, payload):
        """Create stock.move lines to reduce inventory because goods were delivered outside Odoo."""
        StockMove = self.env["stock.move"].sudo()
        StockMoveLine = self.env["stock.move.line"].sudo()

        warehouse = store.default_unshipped_order_warehouse_id or self.env["stock.warehouse"].search(
            [("company_id", "=", store.company_id.id)], limit=1
        )
        stock_loc = warehouse.lot_stock_id if warehouse else self.env.ref("stock.stock_location_stock")
        inv_loc = self.env.ref("stock.stock_location_inventory")

        origin = "%s (Shopify %s)" % (sale_order.name, sale_order.shopify_order_id or "")
        lines = payload.get("line_items") or []

        for line in lines:
            qty = float(line.get("quantity") or 0.0)
            if qty <= 0:
                continue

            sku = (line.get("sku") or "").strip()
            product = False
            if sku:
                product = self.env["product.product"].sudo().search([("default_code", "=", sku)], limit=1)
            if not product:
                # fallback by name
                name = (line.get("name") or "").strip()
                product = self.env["product.product"].sudo().search([("name", "=", name)], limit=1)
            if not product:
                continue

            move = StockMove.create(
                {
                    "name": origin,
                    "product_id": product.id,
                    "product_uom": product.uom_id.id,
                    "product_uom_qty": qty,
                    "location_id": stock_loc.id,
                    "location_dest_id": inv_loc.id,
                    "origin": origin,
                    "company_id": store.company_id.id,
                }
            )
            move._action_confirm()
            move._action_assign()

            StockMoveLine.create(
                {
                    "move_id": move.id,
                    "product_id": product.id,
                    "product_uom_id": product.uom_id.id,
                    "qty_done": qty,
                    "location_id": stock_loc.id,
                    "location_dest_id": inv_loc.id,
                    "company_id": store.company_id.id,
                }
            )
            move._action_done()

    @api.model
    def cancel_order_in_shopify(
        self,
        store,
        shopify_order_id,
        reason="customer",
        message=None,
        email_customer=True,
        sale_order=None,
    ):
        """Cancel Shopify order using the Shopify Order Cancel API."""
        if not store or not shopify_order_id:
            raise UserError(_("Missing Shopify store or Shopify Order ID."))

        if sale_order and sale_order.shopify_cancelled:
            return True

        api_client = store._get_api_client()
        payload = {"reason": reason or "other", "email": bool(email_customer)}

        self._log(
            store,
            _("Shopify API request: cancel order %s") % shopify_order_id,
            payload=json.dumps(payload),
            status="success",
            order_id=shopify_order_id,
            operation="order",
        )

        try:
            res = api_client._request(
                "POST",
                "/orders/%s/cancel.json" % shopify_order_id,
                data=payload,
                max_retries=3,
            )
        except Exception as e:
            msg = str(e)
            # Duplicate protection: Shopify may respond "already cancelled"
            if "already cancelled" in msg.lower() or "already canceled" in msg.lower():
                res = {"skipped": True, "message": msg}
            else:
                self._log(
                    store,
                    _("Shopify cancel failed for order %s: %s") % (shopify_order_id, msg),
                    payload=json.dumps(payload),
                    status="failed",
                    order_id=shopify_order_id,
                    operation="order",
                    response=msg,
                )
                raise

        self._log(
            store,
            _("Shopify order %s cancelled successfully.") % shopify_order_id,
            payload=json.dumps(payload),
            status="success",
            order_id=shopify_order_id,
            operation="order",
            response=json.dumps(res)[:5000] if res else "",
        )

        if sale_order:
            vals = {
                "shopify_cancelled": True,
                "shopify_cancel_reason": reason or "other",
            }
            sale_order.sudo().write(vals)
            if sale_order.state != "cancel":
                try:
                    sale_order.sudo().action_cancel()
                except Exception:
                    sale_order.sudo().write({"state": "cancel"})
        return True

    @api.model
    def refund_order_in_shopify(self, store, shopify_order_id, payload):
        """Create a refund in Shopify using the Refunds API."""
        if not store or not shopify_order_id:
            raise UserError(_("Missing Shopify store or Shopify Order ID."))

        api_client = store._get_api_client()
        try:
            res = api_client._request(
                "POST",
                "/orders/%s/refunds.json" % shopify_order_id,
                data=payload,
                max_retries=3,
            )
        except Exception as e:
            msg = str(e)
            # Duplicate protection: Shopify may indicate refund already exists / cannot be refunded
            if "already refunded" in msg.lower() or "cannot refund" in msg.lower():
                return {"skipped": True, "message": msg}
            self._log(
                store,
                _("Shopify refund failed for order %s: %s") % (shopify_order_id, msg),
                payload=json.dumps(payload),
                status="failed",
                order_id=shopify_order_id,
                operation="order",
                response=msg,
            )
            raise

        self._log(
            store,
            _("Shopify refund created for order %s") % shopify_order_id,
            payload=json.dumps(payload),
            status="success",
            order_id=shopify_order_id,
            operation="order",
            response=json.dumps(res)[:5000] if res else "",
        )
        return res

