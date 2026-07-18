import { Outlet } from "react-router-dom";
import Header from "./Header";
import DisclaimerBanner from "./DisclaimerBanner";

export default function Layout() {
  return (
    <div className="flex min-h-full flex-col">
      <Header />
      <main className="mx-auto w-full max-w-7xl flex-1 px-4 pb-16 pt-6 sm:px-6 lg:px-8">
        <Outlet />
      </main>
      <DisclaimerBanner />
    </div>
  );
}
