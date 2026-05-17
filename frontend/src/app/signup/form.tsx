"use client";

import { useActionState, useState } from "react";
import { signupAction } from "./actions";

type State = { error?: string; needsConfirmation?: boolean } | undefined;

async function action(_prev: State, formData: FormData): Promise<State> {
  return signupAction(formData);
}

export function SignupForm() {
  const [state, formAction, pending] = useActionState<State, FormData>(action, undefined);
  const [showPassword, setShowPassword] = useState(false);

  if (state?.needsConfirmation) {
    return (
      <div
        style={{
          background: "#f5f3f1",
          padding: 20,
          color: "#000",
          fontSize: 14,
          lineHeight: 1.6,
          boxShadow: "rgba(0,0,0,0.075) 0 0 0 0.5px inset",
        }}
      >
        Check your inbox to confirm your email, then come back here to finish
        setting up your profile.
      </div>
    );
  }

  return (
    <form action={formAction} className="flex flex-col gap-6">
      <div>
        <label className="label">Email</label>
        <input
          name="email"
          type="email"
          required
          autoComplete="email"
          className="input"
          placeholder="you@domain.com"
        />
      </div>

      <div>
        <label className="label">Password</label>
        <div style={{ position: "relative" }}>
          <input
            name="password"
            type={showPassword ? "text" : "password"}
            required
            minLength={8}
            autoComplete="new-password"
            className="input"
            placeholder="At least 8 characters"
            style={{ paddingRight: 44 }}
          />
          <button
            type="button"
            onClick={() => setShowPassword((v) => !v)}
            aria-label={showPassword ? "Hide password" : "Show password"}
            tabIndex={-1}
            style={{
              position: "absolute",
              top: 0,
              right: 0,
              height: 44,
              width: 44,
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              background: "transparent",
              border: 0,
              cursor: "pointer",
              color: "#777169",
            }}
          >
            {showPassword ? <EyeOffIcon /> : <EyeIcon />}
          </button>
        </div>
      </div>

      {state?.error ? (
        <p style={{ color: "#ff4704", fontSize: 13 }}>{state.error}</p>
      ) : null}

      <button type="submit" disabled={pending} className="pill pill-primary w-full">
        {pending ? "Creating account…" : "Create account"}
      </button>

      <p style={{ fontSize: 12, color: "#a59f97", lineHeight: 1.5, textAlign: "center" }}>
        By creating an account you agree to apply on your own behalf with truthful
        information.
      </p>
    </form>
  );
}

function EyeIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12Z" />
      <circle cx="12" cy="12" r="3" />
    </svg>
  );
}

function EyeOffIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <path d="M9.88 5.09A10.5 10.5 0 0 1 12 5c6.5 0 10 7 10 7a17.2 17.2 0 0 1-3.22 4.19" />
      <path d="M6.61 6.6A17.4 17.4 0 0 0 2 12s3.5 7 10 7a10.6 10.6 0 0 0 5.39-1.45" />
      <path d="M14.12 14.12a3 3 0 0 1-4.24-4.24" />
      <line x1="3" y1="3" x2="21" y2="21" />
    </svg>
  );
}
