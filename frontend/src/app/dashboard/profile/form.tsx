"use client";

import { useActionState } from "react";
import { saveProfile } from "./actions";

type Profile = {
  full_name: string | null;
  phone: string | null;
  linkedin_url: string | null;
  github_url: string | null;
  portfolio_url: string | null;
  profile_answers: string | null;
};

type State = { ok?: true; error?: string } | undefined;

async function action(_prev: State, formData: FormData): Promise<State> {
  return saveProfile(formData);
}

export function ProfileForm({ initial }: { initial: Profile | null }) {
  const [state, formAction, pending] = useActionState<State, FormData>(action, undefined);
  const p = initial ?? ({} as Partial<Profile>);

  return (
    <form action={formAction} className="flex flex-col gap-8">
      <section className="card flex flex-col gap-5">
        <h2 className="h-card">Contact</h2>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <div>
            <label className="label">Full legal name</label>
            <input name="full_name" defaultValue={p.full_name ?? ""} className="input" required />
          </div>
          <div>
            <label className="label">Phone</label>
            <input name="phone" defaultValue={p.phone ?? ""} className="input" placeholder="+1 555 …" />
          </div>
          <div>
            <label className="label">LinkedIn URL</label>
            <input name="linkedin_url" defaultValue={p.linkedin_url ?? ""} className="input" placeholder="https://linkedin.com/in/…" />
          </div>
          <div>
            <label className="label">GitHub URL</label>
            <input name="github_url" defaultValue={p.github_url ?? ""} className="input" placeholder="https://github.com/…" />
          </div>
          <div className="md:col-span-2">
            <label className="label">Portfolio URL</label>
            <input name="portfolio_url" defaultValue={p.portfolio_url ?? ""} className="input" placeholder="https://…" />
          </div>
        </div>
      </section>

      <section className="card flex flex-col gap-5">
        <h2 className="h-card">Everything else we should know</h2>
        <p style={{ color: "#777169", fontSize: 14, lineHeight: 1.55 }}>
          What you&apos;re looking for, work authorization, sponsorship needs,
          gaps to explain, salary expectations, anything you&apos;d want the agent
          to know when filling an application form.
        </p>
        <p style={{ color: "#777169", fontSize: 12, lineHeight: 1.55 }}>
          Use <span className="mono">## Section</span> headings to organize — the
          onboarding wizard prefills sections like <span className="mono">## Target roles</span>{" "}
          and <span className="mono">## Work authorization</span>. Edit freely.
        </p>
        <textarea
          name="profile_answers"
          defaultValue={p.profile_answers ?? ""}
          className="input input-textarea"
          rows={18}
          style={{ minHeight: 420, lineHeight: 1.5 }}
        />
      </section>

      <div className="flex items-center gap-3">
        <button type="submit" disabled={pending} className="pill pill-primary">
          {pending ? "Saving…" : "Save profile"}
        </button>
        {state?.ok ? (
          <span className="status status-applied">Saved</span>
        ) : state?.error ? (
          <span style={{ color: "var(--color-ember)", fontSize: "var(--text-sm)" }}>{state.error}</span>
        ) : null}
      </div>
    </form>
  );
}
