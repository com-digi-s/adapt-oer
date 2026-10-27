        markSpecificAnswers() {
            let numberOfCorrectAnswers = 0;
            this.get("_items").forEach(item => {
                const answers = item._answers;
                if (!answers)
                    return;
                const userAnswer = item.userAnswer || "";
                const isCorrect = this.checkAnswerIsCorrect(answers, userAnswer);
                item._isCorrect = isCorrect;
                // Persist the index of the same normalized accepted answer
                // that the native grader matched. Keep authored text intact.
                item._answerIndex = answers.findIndex(answer => this.cleanupUserAnswer(answer) === this.cleanupUserAnswer(userAnswer));
                if (!isCorrect)
                    return;
                this.set({
                    _numberOfCorrectAnswers: ++numberOfCorrectAnswers,
                    _isAtLeastOneCorrectSelection: true
                })
            }
            )
        }
