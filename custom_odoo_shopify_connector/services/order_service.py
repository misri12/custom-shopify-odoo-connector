from odoo import _
from odoo.tools import float_round


class OrderService:
    def __init__(self, env, import_service=None):
        self.env = env
        # import_service is optional and used for tax / additional helpers
        self.import_service = import_service

    def create_order_from_payload(self, payload, store=None):
        SaleOrder = self.env["sale.order"]
        Partner = self.env["res.partner"]
        ProductProduct = self.env["product.product"]
        VariantMap = self.env["shopify.variant.map"]

        shopify_order_id = payload.get("id")
        client_ref = "SHOPIFY-%s" % shopify_order_id if shopify_order_id else False

        # Idempotency: prevent duplicate orders for the same Shopify order
        if shopify_order_id:
            existing = SaleOrder.search(
                [("shopify_order_id", "=", str(shopify_order_id))], limit=1
            )
            if existing:
                return existing

            # Also check the dedicated mapping table to be robust against
            # future refactors or manual corrections.
            mapping = (
                self.env["shopify.order.map"]
                .search(
                    [
                        ("store_id", "=", store.id if store else False),
                        ("shopify_order_id", "=", str(shopify_order_id)),
                    ],
                    limit=1,
                )
            )
            if mapping and mapping.odoo_order_id:
                return mapping.odoo_order_id

        if client_ref:
            existing = SaleOrder.search(
                [("client_order_ref", "=", client_ref)], limit=1
            )
            if existing:
                return existing

        customer = payload.get("customer") or {}
        billing = payload.get("billing_address") or {}
        shipping = payload.get("shipping_address") or {}

        partner, partner_invoice, partner_shipping = self._get_or_create_partner(
            payload, customer, billing, shipping, store
        )

        order_vals = {
            "partner_id": partner.id,
            "company_id": store.company_id.id if store and store.company_id else False,
            "client_order_ref": client_ref,
            "origin": payload.get("name"),
            "shopify_order_id": str(shopify_order_id) if shopify_order_id else False,
            "shopify_instance_id": store.id if store else False,
        }

        # Order naming: either use Odoo sequence or Shopify order number with optional prefix
        if store and not store.use_odoo_sequence:
            shopify_name = payload.get("name")  # e.g. "#1044"
            number = (shopify_name or "").lstrip("#") or str(shopify_order_id or "")
            if number:
                prefix = store.order_prefix or ""
                order_vals["name"] = "%s%s" % (prefix, number)

        order = SaleOrder.create(order_vals)

        # Create / update mapping record for easier troubleshooting and
        # robust duplicate protection even across refactors.
        if store and shopify_order_id:
            self.env["shopify.order.map"].sudo().create(
                {
                    "store_id": store.id,
                    "shopify_order_id": str(shopify_order_id),
                    "odoo_order_id": order.id,
                }
            )

        line_items = payload.get("line_items") or []
        for item in line_items:
            sku = item.get("sku")
            quantity = float(item.get("quantity") or 0.0)
            price = float(item.get("price") or 0.0)
            name = item.get("name") or _("Shopify Item")

            product = False
            variant_id = item.get("variant_id")
            shopify_product_id = item.get("product_id")

            # 1) Try explicit variant mapping model
            if variant_id and store:
                mapping = VariantMap.search(
                    [
                        ("store_id", "=", store.id),
                        ("shopify_variant_id", "=", str(variant_id)),
                    ],
                    limit=1,
                )
                if mapping:
                    product = mapping.product_id

            # 2) Fallback to direct product search by Shopify variant id
            if not product and variant_id:
                product = ProductProduct.search(
                    [("shopify_variant_id", "=", str(variant_id))], limit=1
                )

            # 3) Fallback to SKU / internal reference
            if not product and sku:
                product = ProductProduct.search([("default_code", "=", sku)], limit=1)

            # 4) Auto-create product if still not found
            if not product:
                vals = {
                    "name": name,
                    "default_code": sku,
                    "lst_price": price,
                }
                if store and store.auto_create_product_if_not_found:
                    product = ProductProduct.create(vals)
                else:
                    # As a fallback, create a generic product
                    product = ProductProduct.create(vals)

            # Ensure there is a mapping record for this variant when possible
            if (
                store
                and product
                and variant_id
                and shopify_product_id
                and not VariantMap.search(
                    [
                        ("store_id", "=", store.id),
                        ("shopify_variant_id", "=", str(variant_id)),
                    ],
                    limit=1,
                )
            ):
                VariantMap.create(
                    {
                        "store_id": store.id,
                        "product_id": product.id,
                        "shopify_product_id": shopify_product_id,
                        "shopify_variant_id": variant_id,
                    }
                )

            # Improve discount handling: prefer discount_allocations when present
            discount_amount = 0.0
            discount_allocations = item.get("discount_allocations") or []
            if discount_allocations:
                for alloc in discount_allocations:
                    try:
                        discount_amount += float(alloc.get("amount") or 0.0)
                    except Exception:
                        continue
            else:
                discount_amount = float(item.get("total_discount") or 0.0)

            discount_pct = 0.0
            if quantity and price and discount_amount:
                line_total = quantity * price
                if line_total:
                    discount_pct = (discount_amount / line_total) * 100.0

            taxes = self.env["account.tax"]
            if self.import_service and store:
                taxes = self.import_service.get_taxes_for_line(store, item)

            self.env["sale.order.line"].create(
                {
                    "order_id": order.id,
                    "product_id": product.id,
                    "name": name,
                    "product_uom_qty": quantity,
                    "price_unit": float_round(price, 2),
                    "discount": discount_pct,
                    "tax_id": [(6, 0, taxes.ids)],
                }
            )

        # Shipping lines
        shipping_lines = payload.get("shipping_lines") or []
        if store and store.delivery_product_id and shipping_lines:
            for ship in shipping_lines:
                self.env["sale.order.line"].create(
                    {
                        "order_id": order.id,
                        "product_id": store.delivery_product_id.id,
                        "name": ship.get("title") or _("Shipping"),
                        "product_uom_qty": 1.0,
                        "price_unit": float_round(
                            float(ship.get("price") or 0.0), 2
                        ),
                    }
                )

        return order

    def _get_or_create_partner(self, payload, customer, billing, shipping, store):
        Partner = self.env["res.partner"]

        # Fallback to default POS customer when there is no customer on a POS order
        if not customer and store and payload.get("source_name") == "pos":
            if store.default_pos_customer_id:
                partner = store.default_pos_customer_id
                return partner, partner, partner

        email = customer.get("email")
        phone = customer.get("phone")
        first_name = customer.get("first_name") or ""
        last_name = customer.get("last_name") or ""
        name = (first_name + " " + last_name).strip() or email or _("Shopify Customer")

        partner = False
        if email:
            partner = Partner.search([("email", "=", email)], limit=1)
        if not partner and phone:
            partner = Partner.search([("phone", "=", phone)], limit=1)

        vals = {
            "name": name,
            "email": email,
            "phone": phone,
        }

        if billing:
            vals.update(
                {
                    "street": billing.get("address1"),
                    "street2": billing.get("address2"),
                    "city": billing.get("city"),
                    "zip": billing.get("zip"),
                    "country_id": self._get_country_id(billing.get("country_code")),
                    "state_id": self._get_state_id(
                        billing.get("province_code"),
                        billing.get("country_code"),
                    ),
                }
            )

        if partner:
            partner.write(vals)
        else:
            partner = Partner.create(vals)

        # Create / update child contacts for invoice and delivery addresses
        partner_invoice = partner
        partner_shipping = partner

        if billing:
            partner_invoice = self._get_or_create_child_contact(
                partner, billing, "invoice", name or _("Invoice Address")
            )
        if shipping:
            partner_shipping = self._get_or_create_child_contact(
                partner, shipping, "delivery", name or _("Shipping Address")
            )

        return partner, partner_invoice, partner_shipping

    def _get_country_id(self, country_code):
        if not country_code:
            return False
        country = self.env["res.country"].search(
            [("code", "=", country_code)], limit=1
        )
        return country.id or False

    def _get_state_id(self, state_code, country_code):
        if not state_code or not country_code:
            return False
        country = self.env["res.country"].search(
            [("code", "=", country_code)], limit=1
        )
        if not country:
            return False
        state = self.env["res.country.state"].search(
            [("code", "=", state_code), ("country_id", "=", country.id)],
            limit=1,
        )
        return state.id or False

    def _get_or_create_child_contact(self, parent, address, contact_type, default_name):
        Partner = self.env["res.partner"]
        existing = Partner.search(
            [
                ("parent_id", "=", parent.id),
                ("type", "=", contact_type),
                ("street", "=", address.get("address1")),
                ("zip", "=", address.get("zip")),
            ],
            limit=1,
        )
        vals = {
            "parent_id": parent.id,
            "type": contact_type,
            "name": default_name,
            "street": address.get("address1"),
            "street2": address.get("address2"),
            "city": address.get("city"),
            "zip": address.get("zip"),
            "country_id": self._get_country_id(address.get("country_code")),
            "state_id": self._get_state_id(
                address.get("province_code"), address.get("country_code")
            ),
        }
        if existing:
            existing.write(vals)
            return existing
        return Partner.create(vals)

