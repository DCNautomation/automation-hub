import { Route, Routes } from "react-router-dom";
import { RequireAuth } from "./auth";
import { ErrorBoundary, Layout } from "./components";
import Login from "./pages/Login";
import NotFound from "./pages/NotFound";
import RunDetail from "./pages/RunDetail";
import Runs from "./pages/Runs";

export default function App() {
  return (
    <ErrorBoundary>
      <Routes>
        <Route path="/login" element={<Login />} />
        <Route
          element={
            <RequireAuth>
              <Layout />
            </RequireAuth>
          }
        >
          <Route index element={<Runs />} />
          <Route path="runs/:id" element={<RunDetail />} />
          <Route path="*" element={<NotFound />} />
        </Route>
      </Routes>
    </ErrorBoundary>
  );
}
