import { _t } from "@web/core/l10n/translation";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { standardWidgetProps } from "@web/views/widgets/standard_widget_props";

import { Component, useState, onWillUnmount } from "@odoo/owl";

const NOTIFICATION_TYPE = "afs_pos_integration/payment_update";

/**
 * Shown on the invoice while a card Sale OR a Void is pending on the
 * terminal. Both run in a background thread (see afs_payment_wizard.py and
 * models/afs_transaction.py), so this widget is what makes Cancel actually
 * clickable: nothing else on the page is blocked waiting for a synchronous
 * request any more.
 */
export class AfsPendingBanner extends Component {
    static template = "afs_pos_integration.AfsPendingBanner";
    static props = { ...standardWidgetProps };

    setup() {
        this.orm = useService("orm");
        this.busService = useService("bus_service");
        // Stays true after the click: the record remains pending until the
        // terminal answers the cancellation, which takes a few seconds.
        this.state = useState({ cancelling: false });

        this._onNotification = (payload) => this._handleNotification(payload);
        this.busService.subscribe(NOTIFICATION_TYPE, this._onNotification);
        onWillUnmount(() => {
            this.busService.unsubscribe(NOTIFICATION_TYPE, this._onNotification);
        });
    }

    get isPending() {
        return this.props.record.data.afs_state === "pending";
    }

    get isReversing() {
        return this.props.record.data.afs_state === "reversing";
    }

    get message() {
        if (this.state.cancelling) {
            return _t("Cancelling on the terminal…");
        }
        return this.isReversing
            ? _t("Reversing this card payment on the terminal…")
            : _t("Waiting for the customer to tap or insert their card on the terminal…");
    }

    async _handleNotification(payload) {
        if (payload && payload.move_id === this.props.record.resId) {
            this.state.cancelling = false;
            await this.props.record.load();
            this.props.record.model.notify();
        }
    }

    async onCancelClick() {
        this.state.cancelling = true;
        try {
            await this.orm.call("account.move", "action_afs_cancel_pending", [this.props.record.resId]);
        } catch (error) {
            this.state.cancelling = false;
            throw error;
        }
    }
}

export const afsPendingBanner = {
    component: AfsPendingBanner,
};

registry.category("view_widgets").add("afs_pending_banner", afsPendingBanner);
