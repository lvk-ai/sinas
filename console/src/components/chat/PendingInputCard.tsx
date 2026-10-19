import React from 'react';
import { v, tokens } from './tokens';

export interface PendingInputCardProps {
  question: string;
  /** Suggested answers; any free-text answer is accepted too. */
  options: string[] | null;
  expiresAt: string | null;
  /** Resolves true once the answer is recorded. */
  onAnswer: (answer: string) => Promise<boolean>;
}

const QuestionIcon = () => (
  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <circle cx="12" cy="12" r="10" />
    <path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3" />
    <line x1="12" y1="17" x2="12.01" y2="17" />
  </svg>
);

/** An ask_user question the conversation is waiting on, with an answer box. */
export function PendingInputCard({ question, options, expiresAt, onAnswer }: PendingInputCardProps) {
  const [text, setText] = React.useState('');
  const [sending, setSending] = React.useState(false);

  const submit = async (answer: string) => {
    if (!answer.trim() || sending) return;
    setSending(true);
    try {
      if (await onAnswer(answer)) setText('');
    } finally {
      setSending(false);
    }
  };

  const expires = expiresAt ? new Date(expiresAt) : null;

  return (
    <div
      style={{
        borderRadius: v(tokens.radiusMd),
        border: `1px solid ${v(tokens.colorBorderFocus)}`,
        backgroundColor: v(tokens.colorBgElevated),
        overflow: 'hidden',
      }}
    >
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: '8px',
          padding: '10px 14px 6px',
          color: v(tokens.colorPrimary),
          fontSize: '13px',
          fontWeight: 600,
        }}
      >
        <QuestionIcon />
        <span>The agent is asking</span>
      </div>

      <div
        style={{
          padding: '0 14px 10px',
          color: v(tokens.colorText),
          fontSize: '14px',
          lineHeight: 1.5,
          whiteSpace: 'pre-wrap',
          wordBreak: 'break-word',
        }}
      >
        {question}
      </div>

      {options && options.length > 0 && (
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: '6px', padding: '0 14px 10px' }}>
          {options.map((option) => (
            <button
              key={option}
              type="button"
              disabled={sending}
              onClick={() => submit(option)}
              style={{
                padding: '5px 12px',
                borderRadius: v(tokens.radiusSm),
                border: `1px solid ${v(tokens.colorBorder)}`,
                backgroundColor: v(tokens.colorBg),
                color: v(tokens.colorText),
                cursor: sending ? 'default' : 'pointer',
                opacity: sending ? 0.6 : 1,
                fontFamily: 'inherit',
                fontSize: '13px',
              }}
            >
              {option}
            </button>
          ))}
        </div>
      )}

      <form
        onSubmit={(e) => {
          e.preventDefault();
          submit(text);
        }}
        style={{
          display: 'flex',
          gap: '8px',
          padding: '10px 14px',
          borderTop: `1px solid ${v(tokens.colorBorder)}`,
        }}
      >
        <input
          type="text"
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder={options && options.length > 0 ? 'Or type an answer…' : 'Type your answer…'}
          aria-label="Answer"
          disabled={sending}
          style={{
            flex: 1,
            minWidth: 0,
            padding: '7px 10px',
            borderRadius: v(tokens.radiusSm),
            border: `1px solid ${v(tokens.colorBorder)}`,
            backgroundColor: v(tokens.colorBg),
            color: v(tokens.colorText),
            fontFamily: 'inherit',
            fontSize: '13px',
            outline: 'none',
          }}
        />
        <button
          type="submit"
          disabled={sending || !text.trim()}
          style={{
            padding: '7px 14px',
            borderRadius: v(tokens.radiusSm),
            border: 'none',
            backgroundColor: v(tokens.colorPrimary),
            color: v(tokens.colorTextInverse),
            cursor: sending || !text.trim() ? 'default' : 'pointer',
            opacity: sending || !text.trim() ? 0.5 : 1,
            fontFamily: 'inherit',
            fontSize: '13px',
            fontWeight: 500,
          }}
        >
          {sending ? 'Sending…' : 'Answer'}
        </button>
      </form>

      {expires && !Number.isNaN(expires.getTime()) && (
        <div style={{ padding: '0 14px 8px', color: v(tokens.colorTextMuted), fontSize: '11px' }}>
          Unanswered, it expires {expires.toLocaleString()}.
        </div>
      )}
    </div>
  );
}
