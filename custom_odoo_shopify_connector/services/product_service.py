import base64
import logging

import requests

from odoo import _
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)


class ProductService:
    """Business logic for importing and updating Shopify products and variants."""

    def __init__(self, env):
        self.env = env

    def _get_or_create_category(self, product_type):
        """Get or create product.category from Shopify product_type."""
        if not product_type or not str(product_type).strip():
            return self.env["product.category"]
        ProductCategory = self.env["product.category"]
        category = ProductCategory.search([("name", "=", product_type.strip())], limit=1)
        if not category:
            category = ProductCategory.create({"name": product_type.strip()})
        return category

    def _get_or_create_tags(self, tags_string):
        """Parse Shopify tags (comma-separated) and return product.tag recordset."""
        if not tags_string or not str(tags_string).strip():
            return self.env["product.tag"]
        ProductTag = self.env["product.tag"]
        names = [t.strip() for t in str(tags_string).split(",") if t.strip()]
        tag_ids = []
        for name in names:
            tag = ProductTag.search([("name", "=", name)], limit=1)
            if not tag:
                tag = ProductTag.create({"name": name})
            tag_ids.append(tag.id)
        return ProductTag.browse(tag_ids)

    def _download_image_as_base64(self, image_url):
        """Download image from URL and return base64-encoded string, or False on failure."""
        if not image_url or not str(image_url).strip():
            return False
        image_url = str(image_url).strip()
        try:
            headers = {
                "User-Agent": "Odoo-Shopify-Connector/1.0",
                "Accept": "image/*,*/*",
            }
            response = requests.get(
                image_url,
                headers=headers,
                timeout=15,
                allow_redirects=True,
            )
            response.raise_for_status()
            content = response.content
            if not content:
                _logger.warning("Empty response for Shopify image: %s", image_url)
                return False
            return base64.b64encode(content).decode("ascii")
        except Exception as e:
            _logger.warning("Failed to download Shopify image %s: %s", image_url, e)
            return False

    def import_shopify_product(self, payload, store):
        """Create/update product template, variants, attributes, category, images, tags, and mapping."""
        ProductTemplate = self.env["product.template"]
        ProductProduct = self.env["product.product"]
        ProductAttribute = self.env["product.attribute"]
        ProductAttributeValue = self.env["product.attribute.value"]
        ProductMap = self.env["shopify.product.map"]
        VariantMap = self.env["shopify.variant.map"]

        shopify_product_id = payload.get("id")
        title = payload.get("title") or ""
        description = payload.get("body_html") or ""
        product_type = payload.get("product_type") or ""
        tags_string = payload.get("tags") or ""
        vendor = payload.get("vendor") or ""
        variants = payload.get("variants") or []
        options = payload.get("options") or []
        images = payload.get("images") or []
        # Some API responses use singular "image" for the main image
        if not images and payload.get("image"):
            images = [payload["image"]] if isinstance(payload.get("image"), dict) else []

        product_map = ProductMap.search(
            [
                ("store_id", "=", store.id),
                ("shopify_product_id", "=", str(shopify_product_id)),
            ],
            limit=1,
        )

        template = product_map.product_tmpl_id if product_map else False

        if not template:
            template = ProductTemplate.search([("name", "=", title)], limit=1)

        # Category from product_type
        categ = self._get_or_create_category(product_type)
        # Tags from Shopify tags
        tag_ids = self._get_or_create_tags(tags_string)

        template_vals = {
            "name": title or _("Shopify Product"),
            "type": "consu",
            # Odoo 19.0 no longer has the `track_inventory` field on product.template.
            # Stock behavior is controlled via standard inventory configuration instead.
            "description": description,
        }
        if getattr(store, "import_sales_description", True):
            template_vals["description_sale"] = description or False
        if categ:
            template_vals["categ_id"] = categ.id
        if tag_ids:
            template_vals["product_tag_ids"] = [(6, 0, tag_ids.ids)]

        # First variant price for template list_price (fallback)
        first_price = 0.0
        if variants:
            first_price = float(variants[0].get("price") or 0.0)
        template_vals["list_price"] = first_price

        # Weight from first variant if available
        if variants and variants[0].get("weight"):
            try:
                template_vals["weight"] = float(variants[0].get("weight") or 0)
            except (TypeError, ValueError):
                pass

        if template:
            template.write(template_vals)
        else:
            template = ProductTemplate.create(template_vals)

        # Sync product images (first image as main product image)
        if getattr(store, "sync_product_images", True) and template:
            image_url = None
            if images:
                # Shopify product.images[].src
                image_url = images[0].get("src") or images[0].get("source")
            if not image_url and variants:
                # Fallback: first variant can have an image object (e.g. from API)
                first_variant = variants[0] if isinstance(variants[0], dict) else {}
                variant_image = first_variant.get("image")
                if isinstance(variant_image, dict):
                    image_url = variant_image.get("src") or variant_image.get("source")
            if image_url:
                b64 = self._download_image_as_base64(image_url)
                if b64:
                    template.image_1920 = b64
                    _logger.debug("Imported image for product %s (Shopify ID %s)", template.name, shopify_product_id)
                else:
                    _logger.warning("Could not download image for product %s: %s", template.name, image_url)

        if not product_map:
            ProductMap.create(
                {
                    "store_id": store.id,
                    "shopify_product_id": str(shopify_product_id),
                    "product_tmpl_id": template.id,
                }
            )

        attribute_map = {}
        value_map = {}

        for option in options:
            name = option.get("name")
            position = option.get("position")
            if not name or not position:
                continue

            attribute = ProductAttribute.search([("name", "=", name)], limit=1)
            if not attribute:
                attribute = ProductAttribute.create({"name": name})

            attribute_map[position] = attribute

            for value_name in option.get("values") or []:
                key = (attribute.id, value_name)
                if key in value_map:
                    continue
                value = ProductAttributeValue.search(
                    [("name", "=", value_name), ("attribute_id", "=", attribute.id)],
                    limit=1,
                )
                if not value:
                    value = ProductAttributeValue.create(
                        {"name": value_name, "attribute_id": attribute.id}
                    )
                value_map[key] = value

        if attribute_map:
            template.attribute_line_ids = [(5, 0, 0)]
            attribute_lines = []
            for attribute in attribute_map.values():
                values = [
                    v.id
                    for (attr_id, _), v in value_map.items()
                    if attr_id == attribute.id
                ]
                if values:
                    attribute_lines.append(
                        (0, 0, {"attribute_id": attribute.id, "value_ids": [(6, 0, values)]})
                    )
            if attribute_lines:
                template.write({"attribute_line_ids": attribute_lines})

        for variant_payload in variants:
            shopify_variant_id = variant_payload.get("id")
            inventory_item_id = variant_payload.get("inventory_item_id")
            price = float(variant_payload.get("price") or 0.0)
            compare_at_price = variant_payload.get("compare_at_price")
            sku = variant_payload.get("sku") or False
            barcode = variant_payload.get("barcode") or False
            weight = variant_payload.get("weight")

            variant_domain = [("product_tmpl_id", "=", template.id)]
            if sku:
                variant_domain.append(("default_code", "=", sku))

            variant = ProductProduct.search(variant_domain, limit=1)
            variant_vals = {
                "default_code": sku,
                "lst_price": price,
                "barcode": barcode or False,
            }
            if weight is not None:
                try:
                    variant_vals["weight"] = float(weight)
                except (TypeError, ValueError):
                    pass

            if not variant:
                variant_vals["product_tmpl_id"] = template.id
                variant = ProductProduct.create(variant_vals)
            else:
                variant.write(variant_vals)

            # Resolve product.template.attribute.value IDs (not product.attribute.value)
            ptav_ids = []
            if attribute_map:
                for position, attribute in attribute_map.items():
                    option_key = "option%s" % position
                    option_value_name = variant_payload.get(option_key)
                    if not option_value_name:
                        continue
                    pav = value_map.get((attribute.id, option_value_name))
                    if not pav:
                        continue
                    # Template attribute values are created with attribute_line_ids; find the ptav for this template
                    ptav = self.env["product.template.attribute.value"].search(
                        [
                            ("product_tmpl_id", "=", template.id),
                            ("product_attribute_value_id", "=", pav.id),
                        ],
                        limit=1,
                    )
                    if ptav:
                        ptav_ids.append(ptav.id)
            if ptav_ids:
                variant.write({"product_template_attribute_value_ids": [(6, 0, ptav_ids)]})

            vmap = VariantMap.search(
                [
                    ("store_id", "=", store.id),
                    ("shopify_variant_id", "=", str(shopify_variant_id)),
                ],
                limit=1,
            )
            vals_map = {
                "store_id": store.id,
                "shopify_product_id": str(shopify_product_id),
                "shopify_variant_id": str(shopify_variant_id),
                "shopify_inventory_item_id": str(inventory_item_id)
                if inventory_item_id
                else False,
                "product_id": variant.id,
            }
            if vmap:
                vmap.write(vals_map)
            else:
                VariantMap.create(vals_map)

    def export_product_to_shopify(
        self,
        product_tmpl,
        store,
        layer=None,
        export_name=True,
        export_description=True,
        export_tags=True,
        export_categories=True,
        export_price=True,
        export_image=True,
        publish_option="web_only",
    ):
        """Build Shopify product payload from Odoo product and export (create or update) via API.

        :param product_tmpl: product.template record
        :param store: shopify.store record (must have pricelist_id for export_price)
        :param layer: optional shopify.product.layer for overrides and existing shopify_product_id
        :param publish_option: 'web_only' | 'web_pos' | 'not_published'
        :returns: dict with keys shopify_product_id, created (bool)
        """
        api_client = store._get_api_client()
        product_tmpl.ensure_one()
        store.ensure_one()

        # Validate product before export: do not export services
        if product_tmpl.type == "service":
            raise ValidationError(
                _("Product '%s' is a Service and cannot be exported to Shopify.")
                % product_tmpl.name
            )

        # Resolve description: layer override or product
        description = ""
        if layer and layer.description_override:
            description = layer.description_override
        elif export_description:
            # Prefer sales description, then internal description, finally fall back to name
            description = (
                product_tmpl.description_sale
                or product_tmpl.description
                or product_tmpl.name
                or ""
            )

        # Title
        title = product_tmpl.name if export_name else ""

        # Tags: comma-separated
        tags_str = ""
        if export_tags and product_tmpl.product_tag_ids:
            tags_str = ",".join(product_tmpl.product_tag_ids.mapped("name"))

        # Product type from category
        product_type = ""
        if export_categories and product_tmpl.categ_id:
            product_type = product_tmpl.categ_id.name or ""

        # Build options (attribute names and values) for multi-variant products
        attr_lines = product_tmpl.attribute_line_ids.sorted(key=lambda l: l.id)
        options_payload = []
        for line in attr_lines:
            values = line.product_template_value_ids.mapped(
                "product_attribute_value_id.name"
            )
            if values:
                options_payload.append({"name": line.attribute_id.name, "values": values})

        # Build one variant per product.product with SKU and price from pricelist
        variant_records = product_tmpl.product_variant_ids.sorted(key=lambda p: p.id)
        if not variant_records:
            raise ValidationError(
                _("Product '%s' has no variants. Create at least one variant.")
                % product_tmpl.name
            )

        for v in variant_records:
            sku_v = (v.default_code or "").strip() or None
            if not sku_v:
                raise ValidationError(
                    _(
                        "Product '%s' (variant: %s) has no Internal Reference (SKU). "
                        "Set the Internal Reference on each variant before exporting to Shopify."
                    )
                    % (product_tmpl.name, v.display_name)
                )

        variants = []
        for v in variant_records:
            price = "0.00"
            if export_price and store.pricelist_id:
                price = store.pricelist_id._get_product_price(v, 1.0)
            price = "%.2f" % price
            sku_v = (v.default_code or "").strip() or None

            # Push stock information to Shopify so inventory is tracked there
            qty = int(v.qty_available or 0)
            var_payload = {
                "price": price,
                "sku": sku_v,
                "taxable": True,
                "inventory_management": "shopify",
                "inventory_policy": "deny",
                "inventory_quantity": qty,
            }
            # Option1, option2, option3 for Shopify (match options order)
            for idx, line in enumerate(attr_lines):
                ptav = v.product_template_attribute_value_ids.filtered(
                    lambda p: p.attribute_line_id == line
                )[:1]
                if ptav:
                    val_name = ptav.product_attribute_value_id.name
                    var_payload["option%s" % (idx + 1)] = val_name
            variants.append(var_payload)

        # Build product payload (options required for multi-option products in Shopify)
        payload = {
            "title": title or _("Product"),
            "body_html": description,
            "product_type": product_type,
            "tags": tags_str,
            "variants": variants,
        }
        if options_payload:
            payload["options"] = options_payload

        # Status and published_scope
        if publish_option == "not_published":
            payload["status"] = "draft"
            payload["published_scope"] = "web"
        else:
            payload["status"] = "active"
            payload["published_scope"] = "global" if publish_option == "web_pos" else "web"

        # Image: first product image as base64 (Shopify expects "attachment" as base64 string)
        if export_image and product_tmpl.image_1920:
            img = product_tmpl.image_1920
            if isinstance(img, bytes):
                img = img.decode("utf-8")
            attachment = (img or "").replace("\n", "").strip()
            if attachment:
                payload["images"] = [{"attachment": attachment}]

        existing_id = layer.shopify_product_id if layer else None
        if existing_id:
            response = api_client.update_product(existing_id, payload)
            created = False
        else:
            response = api_client.create_product(payload)
            created = True

        res_product = response.get("product") or response
        shopify_id = str(res_product.get("id")) if res_product.get("id") else None
        if not shopify_id:
            raise ValueError("Shopify API did not return product id")

        # Keep mapping in sync: create or update shopify.product.map and variant map
        ProductMap = self.env["shopify.product.map"]
        VariantMap = self.env["shopify.variant.map"]
        product_map = ProductMap.search(
            [("store_id", "=", store.id), ("shopify_product_id", "=", shopify_id)],
            limit=1,
        )
        if not product_map:
            ProductMap.create({
                "store_id": store.id,
                "shopify_product_id": shopify_id,
                "product_tmpl_id": product_tmpl.id,
            })
        else:
            product_map.product_tmpl_id = product_tmpl.id

        variants_res = res_product.get("variants") or []
        variant_records_sorted = product_tmpl.product_variant_ids.sorted(key=lambda p: p.id)
        for i, v in enumerate(variants_res):
            vid = str(v.get("id"))
            inv_item_id = v.get("inventory_item_id")
            product_product = (
                variant_records_sorted[i] if i < len(variant_records_sorted) else None
            )
            if not product_product:
                continue
            vmap = VariantMap.search(
                [("store_id", "=", store.id), ("shopify_variant_id", "=", vid)],
                limit=1,
            )
            vals = {
                "store_id": store.id,
                "shopify_product_id": shopify_id,
                "shopify_variant_id": vid,
                "shopify_inventory_item_id": str(inv_item_id) if inv_item_id else False,
                "product_id": product_product.id,
            }
            if vmap:
                vmap.write(vals)
            else:
                VariantMap.create(vals)

        return {"shopify_product_id": shopify_id, "created": created}
