from odoo import fields, models


class ProductTemplate(models.Model):
    _inherit = "product.template"

    shopify_product_id = fields.Char(
        string="Shopify Product ID",
        help=(
            "Primary Shopify product id for this template (for the main Shopify store). "
            "For multi-store setups, detailed mappings are stored in Shopify product "
            "mapping models and on Shopify product layers."
        ),
    )

