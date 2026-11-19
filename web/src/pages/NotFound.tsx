import { Link } from "react-router-dom";

export default function NotFound({ what = "page" }: { what?: string }) {
  return (
    <main className="page narrow">
      <h1>No such {what}</h1>
      <p>The address may be mistyped, or the {what} was never there.</p>
      <Link className="button" to="/">
        Back to runs
      </Link>
    </main>
  );
}
