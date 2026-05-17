"use server";

import { revalidatePath } from "next/cache";
import { redirect } from "next/navigation";
import { createClient } from "@/lib/supabase/server";
import { isDevBypass } from "@/lib/devflags";

export async function loginAction(formData: FormData): Promise<{ error: string } | undefined> {
  const next = String(formData.get("next") ?? "/dashboard");

  // Dev bypass: pretend login succeeded.
  if (isDevBypass()) {
    revalidatePath("/", "layout");
    redirect(next);
  }

  const email = String(formData.get("email") ?? "").trim();
  const password = String(formData.get("password") ?? "");

  if (!email || !password) return { error: "Email and password are required." };

  const supabase = await createClient();
  const { error } = await supabase.auth.signInWithPassword({ email, password });
  if (error) return { error: error.message };

  revalidatePath("/", "layout");
  redirect(next);
}
