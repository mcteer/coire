import { Children, useEffect, type ReactNode } from "react";
import Markdown from "react-markdown";
import runbook from "virtual:training-runbook";

function anchor(children: ReactNode): string {
  return Children.toArray(children)
    .join("")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-|-$/g, "");
}

export function TrainingRunbook({ isAdmin }: { isAdmin: boolean }) {
  useEffect(() => {
    if (isAdmin && location.hash) document.getElementById(location.hash.slice(1))?.scrollIntoView();
  }, [isAdmin]);
  if (!isAdmin)
    return (
      <main className="app">
        <h1>Admin access required</h1>
      </main>
    );
  return (
    <main className="app">
      <article className="panel glass">
        <a href="/#training">Back to Training</a>
        <Markdown
          components={{
            h2: ({ children }) => <h2 id={anchor(children)}>{children}</h2>,
            h3: ({ children }) => <h3 id={anchor(children)}>{children}</h3>,
          }}
        >
          {runbook}
        </Markdown>
      </article>
    </main>
  );
}
