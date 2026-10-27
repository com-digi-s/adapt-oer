        isCorrect() {
            // A real all-wrong response has zero correct items, not undefined
            // (NaN after weighting) or a stale count from an earlier marking.
            this.set({
                _numberOfCorrectAnswers: 0,
                _isAtLeastOneCorrectSelection: false
            });
            if (this.get("_answers")) {
                this.markGenericAnswers()
            } else {
                this.markSpecificAnswers()
            }
            return this.get("_items").every(_ref3 => {
                let {_isCorrect} = _ref3;
                return _isCorrect
            }
            )
        }
