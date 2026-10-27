        onComponentViewRendered: function(view) {
            if (!view.$el.hasClass("is-question"))
                return;
            const parentArticleId = view.model.findAncestor("articles").get("_id");
            const submitAllArticleId = this.model.get("_articleView").model.get("_id");
            if (parentArticleId !== submitAllArticleId)
                return;
            this.model.get("_componentViews").push(view);
            if (view.model.get("_component") === "textinput") {
                view.$el.find("input").on("change.submitAll", this.onInteraction);
                this.onInteraction();
                return;
            }
            // Genuine native controls include drag/drop and keyboard-driven
            // changes. Recheck after their real event handlers have updated
            // canSubmit, using the existing deferred interaction delegate.
            view.$el.on("click.submitAll change.submitAll dragstop.submitAll", this.onInteraction);
            // Native canSubmit must run after deferred Core draft restoration,
            // not wait for an extra click in an already restored answer.
            this.onInteraction();
        },
