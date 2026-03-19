from odoo import api, fields, models, _


class ProductProduct(models.Model):
    _inherit = "product.product"

    shopify_stock_type = fields.Selection(
        [
            ("fixed", "Fixed"),
            ("percentage", "Percentage"),
        ],
        string="Shopify Stock Export Type",
        help="If set, overrides the instance stock configuration for this product when exporting stock to Shopify.",
    )
    shopify_stock_value = fields.Float(
        string="Shopify Stock Value",
        help="If type is Fixed, this exact quantity will be exported. If Percentage, this percentage of the computed stock will be exported.",
    )

    shopify_product_id = fields.Char(
        string="Shopify Product ID",
        help="Primary Shopify product id for this variant (for the main Shopify store). For multi-store setups, detailed mappings are stored in Shopify product mapping models.",
    )
    shopify_variant_id = fields.Char(
        string="Shopify Variant ID",
        help="Primary Shopify variant id for this product (for the main Shopify store). For multi-store setups, detailed mappings are stored in Shopify variant mapping models.",
    )

    def action_shopify_update_product(self):
        """Open the Shopify Update Product wizard for the selected variants."""
        self.ensure_one()
        action = self.env["ir.actions.act_window"]._for_xml_id(
            "custom_odoo_shopify_connector.action_shopify_update_product_wizard"
        )
        ctx = dict(self.env.context or {})
        ctx.update(
            {
                "active_model": "product.product",
                "active_ids": self.ids,
            }
        )
        action["context"] = ctx
        return action

    def action_shopify_export_stock(self):
        """Open the Shopify stock export wizard scoped to the selected products."""
        self.ensure_one()
        action = self.env["ir.actions.act_window"]._for_xml_id(
            "custom_odoo_shopify_connector.action_shopify_export_stock_wizard"
        )
        ctx = dict(self.env.context or {})
        ctx.update(
            {
                "active_model": "product.product",
                "active_ids": self.ids,
            }
        )
        action["context"] = ctx
        return action

