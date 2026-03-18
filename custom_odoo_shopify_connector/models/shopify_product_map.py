from odoo import fields, models


class ShopifyProductMap(models.Model):
    _name = "shopify.product.map"
    _description = "Shopify Product Template Mapping"

    store_id = fields.Many2one("shopify.store", required=True, ondelete="cascade")
    shopify_product_id = fields.Char(required=True, index=True)
    product_tmpl_id = fields.Many2one(
        "product.template",
        string="Odoo Product Template",
        required=True,
        ondelete="cascade",
    )

    _shopify_product_store_unique = models.Constraint(
        "UNIQUE(store_id, shopify_product_id)",
        "Each Shopify product must be mapped only once per store.",
    )

