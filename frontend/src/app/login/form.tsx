"use client";

import { useActionState } from "react";
import { loginAction } from "./actions";

type State = { error: string } | undefined;

async function action(_prev: State, formData: FormData): Promise<State> {
  return loginAction(formData);
}

export function LoginForm({ next }: { next: string }) {
  const [state, formAction, pending] = useActionState<State, FormData>(action, undefined);
  return (
    <form action={formAction} className="flex flex-col gap-6">
      <input type="hidden" name="next" value={next} />

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
        <div className="flex items-baseline justify-between">
          <label className="label">Password</label>
          <a
            href="#"
            style={{ fontSize: 12, color: "#777169", textDecoration: "underline", textUnderlineOffset: 3 }}
          >
            Forgot?
          </a>
        </div>
        <input
          name="password"
          type="password"
          required
          autoComplete="current-password"
          className="input"
          placeholder="••••••••"
        />
      </div>

      {state?.error ? (
        <p style={{ color: "#ff4704", fontSize: 13 }}>{state.error}</p>
      ) : null}

      <button type="submit" disabled={pending} className="pill pill-primary w-full">
        {pending ? "Signing in…" : "Sign in"}
      </button>
    </form>
  );
}
