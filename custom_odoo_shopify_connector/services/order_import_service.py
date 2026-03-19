from odoo import _, fields


class OrderImportService:
    def __init__(self, env):
        self.env = env

    def _log(self, store, message, payload, status="success"):
        self.env["shopify.sync.log.mixin"].create_log(
            store=store,
            log_type="order",
            message=message,
            payload=payload,
            status=status,
        )

    def _extract_gateway_name(self, payload):
        """Extract the payment gateway identifier from the Shopify payload."""
        gateway = payload.get("gateway")
        if gateway:
            return gateway

        payment_gateway_names = payload.get("payment_gateway_names")
        # Shopify can send this either as a list or as a string
        if isinstance(payment_gateway_names, list) and payment_gateway_names:
            return payment_gateway_names[0]
        if isinstance(payment_gateway_names, str) and payment_gateway_names:
            return payment_gateway_names
        return False

    def get_financial_workflow(self, store, payload):
        """Return the workflow configured for the given payload or False if
        the order should not be imported according to the financial rules.
        """
        gateway_name = self._extract_gateway_name(payload or {}) or False
        financial_status = (payload or {}).get("financial_status")

        if not gateway_name or not financial_status:
            # No gateway or financial status -> do not import
            return False

        gateway = (
            self.env["shopify.payment.gateway"]
            .search(
                [
                    ("instance_id", "=", store.id),
                    ("active", "=", True),
                    ("payment_code", "=", gateway_name),
                ],
                limit=1,
            )
        )
        if not gateway:
            return False

        rule = (
            self.env["shopify.financial.status"]
            .search(
                [
                    ("instance_id", "=", store.id),
                    ("payment_gateway_id", "=", gateway.id),
                    ("shopify_financial_status", "=", financial_status),
                    ("active", "=", True),
                ],
                limit=1,
            )
        )
        if not rule or not rule.workflow_id:
            return False

        return rule.workflow_id

    def should_import_order(self, store, payload):
        """Return True if the order should be imported based on fulfillment status
        configuration on the Shopify store.
        """
        if not store:
            return False
        status_cfg = store.import_order_status or "unshipped"
        fulfillment_status = (payload or {}).get("fulfillment_status")
        if status_cfg == "unshipped":
            # Only orders where fulfillment_status is not set
            return not fulfillment_status
        # partially_fulfilled: allow None and 'partial'
        return fulfillment_status in (None, "partial")

    def _find_or_create_tax(self, store, tax_rate, name_hint=None):
        """Find an existing tax by percentage (and use_odoo_tax behavior) or
        create one when allowed by configuration.
        """
        Tax = self.env["account.tax"]
        domain = [
            ("type_tax_use", "in", ["sale", "all"]),
            ("amount_type", "=", "percent"),
            ("amount", "=", tax_rate),
            ("company_id", "=", store.company_id.id),
        ]
        existing = Tax.search(domain, limit=1)
        if existing:
            return existing

        if store.shopify_tax_behavior != "create_tax_if_not_found":
            return Tax.browse()

        name = name_hint or _("Shopify Tax %s%%") % tax_rate
        return Tax.create(
            {
                "name": name,
                "amount_type": "percent",
                "amount": tax_rate,
                "type_tax_use": "sale",
                "company_id": store.company_id.id,
            }
        )

    def get_taxes_for_line(self, store, line):
        """Return account.tax recordset for a given Shopify line item according
        to the configured tax behavior.
        """
        Tax = self.env["account.tax"]
        if not store:
            return Tax.browse()

        tax_lines = line.get("tax_lines") or []
        if not tax_lines:
            return Tax.browse()

        # Shopify tax_lines can contain multiple entries; sum their rates
        total_rate = 0.0
        name_hint = None
        for tax in tax_lines:
            rate = float(tax.get("rate") or 0.0) * 100.0
            total_rate += rate
            if not name_hint:
                name_hint = tax.get("title")

        if not total_rate:
            return Tax.browse()

        return self._find_or_create_tax(store, total_rate, name_hint=name_hint)

    def apply_workflow(self, order, store, payload=None, workflow=None):
        """Apply the configured sales auto workflow on the sale order.

        If a workflow is provided (for example from financial status rules)
        it takes precedence. Otherwise falls back to store configuration.
        """
        workflow = workflow or store.sale_auto_workflow_id
        if not workflow or not order:
            return

        # Apply shipment policy on the sale order
        if workflow.shipment_policy == "deliver_each_product":
            order.picking_policy = "direct"
        elif workflow.shipment_policy == "deliver_all_at_once":
            order.picking_policy = "one"

        payload_data = payload or {"order_id": order.id}

        # STEP 2: Confirm quotation
        if workflow.confirm_quotation and order.state in ("draft", "sent"):
            try:
                order.action_confirm()
                self._log(
                    store,
                    _("Sale order %s confirmed by auto workflow.") % order.name,
                    payload_data,
                    "success",
                )
            except Exception as e:
                self._log(
                    store,
                    _("Failed to confirm sale order %s: %s") % (order.name, e),
                    payload_data,
                    "failed",
                )
                return

        invoices = self.env["account.move"]

        # STEP 3: Create invoice
        if workflow.create_invoice and order.state in ("sale", "done"):
            try:
                invoices = order._create_invoices()

                # Optionally force accounting date and sales journal
                if invoices:
                    if workflow.force_accounting_date and order.date_order:
                        for inv in invoices:
                            inv.invoice_date = fields.Date.to_date(order.date_order)
                    if workflow.sales_journal_id:
                        invoices.write({"journal_id": workflow.sales_journal_id.id})

                self._log(
                    store,
                    _("Invoice(s) created for sale order %s by auto workflow.")
                    % order.name,
                    {"order_id": order.id, "invoice_ids": invoices.ids},
                    "success",
                )
            except Exception as e:
                self._log(
                    store,
                    _("Failed to create invoices for sale order %s: %s")
                    % (order.name, e),
                    payload_data,
                    "failed",
                )
                return

        # STEP 4: Validate invoice
        if workflow.validate_invoice and invoices:
            try:
                invoices.action_post()
                self._log(
                    store,
                    _("Invoice(s) validated for sale order %s by auto workflow.")
                    % order.name,
                    {"order_id": order.id, "invoice_ids": invoices.ids},
                    "success",
                )
            except Exception as e:
                self._log(
                    store,
                    _("Failed to validate invoices for sale order %s: %s")
                    % (order.name, e),
                    {"order_id": order.id, "invoice_ids": invoices.ids},
                    "failed",
                )
                return

        # STEP 5: Register payment
        if workflow.register_payment and invoices:
            payment_journal = workflow.payment_journal_id
            payment_method = workflow.payment_method_id

            # Prefer payment journal configured on the Shopify payment gateway
            # when the order is fully paid and a matching gateway configuration exists.
            if payload:
                gateway_name = self._extract_gateway_name(payload) or False
                if gateway_name:
                    gateway = (
                        self.env["shopify.payment.gateway"]
                        .search(
                            [
                                ("instance_id", "=", store.id),
                                ("active", "=", True),
                                ("payment_code", "=", gateway_name),
                            ],
                            limit=1,
                        )
                    )
                    if gateway and gateway.odoo_journal_id:
                        payment_journal = gateway.odoo_journal_id

            if not payment_journal or not payment_method:
                self._log(
                    store,
                    _(
                        "Payment registration skipped for sale order %s because "
                        "payment journal or method is not configured on the workflow."
                    )
                    % order.name,
                    {"order_id": order.id, "invoice_ids": invoices.ids},
                    "failed",
                )
                return

            try:
                amount = sum(invoices.mapped("amount_residual"))
                if not amount:
                    return

                payment_vals = {
                    "payment_type": "inbound",
                    "partner_type": "customer",
                    "partner_id": order.partner_id.id,
                    "amount": amount,
                    "currency_id": order.currency_id.id,
                    "journal_id": payment_journal.id,
                    "payment_method_id": payment_method.id,
                    "ref": _("Payment for Shopify order %s") % (order.shopify_order_id or order.name),
                    "date": fields.Date.context_today(self.env.user),
                }
                payment = self.env["account.payment"].create(payment_vals)
                payment.action_post()

                self._log(
                    store,
                    _("Payment registered for sale order %s by auto workflow.")
                    % order.name,
                    {
                        "order_id": order.id,
                        "invoice_ids": invoices.ids,
                        "payment_id": payment.id,
                    },
                    "success",
                )
            except Exception as e:
                self._log(
                    store,
                    _("Failed to register payment for sale order %s: %s")
                    % (order.name, e),
                    {"order_id": order.id, "invoice_ids": invoices.ids},
                    "failed",
                )

