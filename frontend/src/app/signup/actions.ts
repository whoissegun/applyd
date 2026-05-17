"use server";

import { revalidatePath } from "next/cache";
import { redirect } from "next/navigation";
import { headers } from "next/headers";
import { createClient } from "@/lib/supabase/server";
import { isDevBypass } from "@/lib/devflags";

export async function signupAction(
  formData: FormData,
): Promise<{ error?: string; needsConfirmation?: boolean } | undefined> {
  // Dev bypass: pretend signup succeeded and drop user into onboarding.
  if (isDevBypass()) {
    revalidatePath("/", "layout");
    redirect("/onboarding");
  }

  const email = String(formData.get("email") ?? "").trim();
  const password = String(formData.get("password") ?? "");

  if (!email || !password)
    return { error: "Email and password are required." };
  if (password.length < 8)
    return { error: "Password must be at least 8 characters." };

  const supabase = await createClient();
  const hdrs = await headers();
  const origin =
    hdrs.get("origin") ?? `https://${hdrs.get("host") ?? "localhost:3000"}`;

  // No `full_name` here — we collect it during onboarding. The DB trigger
  // stores NULL via nullif() when it's missing.
  const { data, error } = await supabase.auth.signUp({
    email,
    password,
    options: {
      emailRedirectTo: `${origin}/auth/callback`,
    },
  });
  if (error) return { error: error.message };

  // If email-confirmation is on, there's no session yet.
  if (!data.session) return { needsConfirmation: true };

  revalidatePath("/", "layout");
  redirect("/onboarding");
}
