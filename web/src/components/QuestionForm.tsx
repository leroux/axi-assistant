import { useCallback, useState } from "react";
import type { QuestionData } from "../types/messages";

interface Props {
  agent: string;
  questions: QuestionData[];
  onSubmit: (agent: string, answers: Record<string, string>) => void;
}

export function QuestionForm({ agent, questions, onSubmit }: Props) {
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const [otherText, setOtherText] = useState<Record<string, string>>({});
  const [otherSelected, setOtherSelected] = useState<Record<string, boolean>>({});

  const handleSelect = useCallback((question: string, value: string, multiSelect?: boolean) => {
    setAnswers((prev) => {
      if (!multiSelect) return { ...prev, [question]: value };
      // Multi-select: toggle comma-separated values
      const current = prev[question] ? prev[question].split(", ") : [];
      const idx = current.indexOf(value);
      if (idx >= 0) {
        current.splice(idx, 1);
      } else {
        current.push(value);
      }
      return { ...prev, [question]: current.join(", ") };
    });
    // Deselect "Other" when picking a predefined option (single-select only)
    if (!multiSelect) {
      setOtherSelected((prev) => ({ ...prev, [question]: false }));
    }
  }, []);

  const handleOtherToggle = useCallback((question: string, multiSelect?: boolean) => {
    setOtherSelected((prev) => {
      const nowSelected = !prev[question];
      if (!multiSelect && nowSelected) {
        // Single-select: set answer to other text
        setAnswers((a) => ({ ...a, [question]: otherText[question] || "" }));
      } else if (!nowSelected) {
        // Deselected other: clear its contribution
        if (!multiSelect) {
          setAnswers((a) => ({ ...a, [question]: "" }));
        }
      }
      return { ...prev, [question]: nowSelected };
    });
  }, [otherText]);

  const handleOtherText = useCallback((question: string, text: string, multiSelect?: boolean) => {
    setOtherText((prev) => ({ ...prev, [question]: text }));
    if (!multiSelect) {
      setAnswers((prev) => ({ ...prev, [question]: text }));
    }
  }, []);

  const handleSubmit = useCallback(() => {
    // Merge "Other" text into multi-select answers
    const finalAnswers = { ...answers };
    for (const q of questions) {
      if (q.multiSelect && otherSelected[q.question] && otherText[q.question]) {
        const current = finalAnswers[q.question] ? finalAnswers[q.question].split(", ") : [];
        current.push(otherText[q.question]);
        finalAnswers[q.question] = current.join(", ");
      }
    }
    onSubmit(agent, finalAnswers);
  }, [agent, answers, questions, otherSelected, otherText, onSubmit]);

  const allAnswered = questions.every((q) => {
    if (otherSelected[q.question]) return !!otherText[q.question];
    return !!answers[q.question];
  });

  return (
    <div className="question-form" data-testid="question-form">
      <div className="question-header">Question from agent</div>
      {questions.map((q, qi) => (
        <div key={qi} className="question-block" data-testid={`question-block-${qi}`}>
          {q.header && <span className="question-tag">{q.header}</span>}
          <div className="question-text">{q.question}</div>
          <div className="question-options">
            {q.options.map((opt, oi) => {
              const isChecked = q.multiSelect
                ? (answers[q.question] || "").split(", ").includes(opt.label)
                : answers[q.question] === opt.label && !otherSelected[q.question];
              return (
                <label key={oi} className="question-option">
                  <input
                    type={q.multiSelect ? "checkbox" : "radio"}
                    name={`q-${qi}`}
                    checked={isChecked}
                    onChange={() => handleSelect(q.question, opt.label, q.multiSelect)}
                  />
                  <span className="option-label">{opt.label}</span>
                  {opt.description && (
                    <span className="option-desc">{opt.description}</span>
                  )}
                </label>
              );
            })}
            {/* "Other" free-text option */}
            <label className="question-option">
              <input
                type={q.multiSelect ? "checkbox" : "radio"}
                name={`q-${qi}`}
                checked={otherSelected[q.question] || false}
                onChange={() => handleOtherToggle(q.question, q.multiSelect)}
              />
              <span className="option-label">Other</span>
            </label>
            {otherSelected[q.question] && (
              <input
                type="text"
                className="other-input"
                placeholder="Type your answer..."
                value={otherText[q.question] || ""}
                onChange={(e) => handleOtherText(q.question, e.target.value, q.multiSelect)}
                autoFocus
              />
            )}
          </div>
        </div>
      ))}
      <button
        className="btn-submit-answers"
        onClick={handleSubmit}
        disabled={!allAnswered}
      >
        Submit Answers
      </button>
    </div>
  );
}
