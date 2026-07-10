import { cookies } from "next/headers";
import { redirect } from "next/navigation";

import { Dashboard } from "../components/dashboard";
import { SESSION_COOKIE, verifySessionToken } from "../lib/auth";

export const dynamic = "force-dynamic";

export default async function HomePage() {
  const cookieStore = await cookies();
  const session = verifySessionToken(
    cookieStore.get(SESSION_COOKIE)?.value,
    Math.floor(Date.now() / 1000),
  );
  if (session === null) {
    redirect("/login");
  }
  return <Dashboard operator={session.operator} role={session.role} />;
}
