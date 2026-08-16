import { Link, Outlet } from "react-router-dom";

export function Shell() {
  return (
    <>
      <header className="shell-header">
        <Link to="/" className="shell-brand">
          Order pipeline
        </Link>
        <nav className="shell-nav">
          <Link to="/">Watch</Link>
          <a href="/control" target="_blank" rel="noreferrer">
            Controls
          </a>
        </nav>
      </header>
      <Outlet />
    </>
  );
}
