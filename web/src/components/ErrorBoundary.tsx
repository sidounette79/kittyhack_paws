import { Component, type ErrorInfo, type ReactNode } from "react";

// 07.10, Sid ("swipe gauche... écran noir toujours. Si je fais avec la
// flèche aussi... ça fait aussi avec Boubou/Nala"): reproducible on every
// cat via a plain button click (not just a gesture), so this is almost
// certainly a real JS exception during the page transition - but there's
// no way to read the Android WebView's console from here. Without this,
// an uncaught render error just leaves React's root empty, and the dark
// body background underneath reads as a plain black screen with zero
// information. This shows the actual error on screen instead, so the next
// crash is diagnosable from a screenshot rather than a guess.
export default class ErrorBoundary extends Component<
  { children: ReactNode },
  { error: Error | null }
> {
  state = { error: null as Error | null };

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("[ErrorBoundary]", error, info.componentStack);
  }

  render() {
    if (this.state.error) {
      return (
        <div className="min-h-screen bg-black p-4 text-white">
          <h1 className="mb-2 text-lg font-semibold text-red-300">Une erreur est survenue</h1>
          <p className="mb-3 text-sm text-white/70">
            Fais une capture d'écran de ce message pour Claude.
          </p>
          <pre className="whitespace-pre-wrap break-words rounded-lg bg-white/5 p-3 text-xs text-white/80">
            {this.state.error.message}
            {"\n\n"}
            {this.state.error.stack}
          </pre>
          <button
            onClick={() => (window.location.href = "/presence/")}
            className="mt-4 rounded-md bg-white px-4 py-2 text-sm font-medium text-black"
          >
            Retour à Chats
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}
