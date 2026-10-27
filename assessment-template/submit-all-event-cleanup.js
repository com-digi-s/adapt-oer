        removeEventListeners: function() {
            this.model.get("_componentViews").forEach(view => {
                view.$el.find("input").off(".submitAll");
                view.$el.off(".submitAll");
            });
        },
