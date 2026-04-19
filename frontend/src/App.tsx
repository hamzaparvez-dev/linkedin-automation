import { HashRouter, NavLink, Navigate, Route, Routes } from 'react-router-dom'
import './App.css'
import { Overview } from './pages/Overview'
import { Leads } from './pages/Leads'
import { Automation } from './pages/Automation'
import { AccountsPage } from './pages/AccountsPage'
import { CsvLeads } from './pages/CsvLeads'

function Layout() {
  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="brand">Leadgen · Operations</div>
        <nav className="nav">
          <NavLink end to="/">
            Overview
          </NavLink>
          <NavLink to="/leads">Leads (DB)</NavLink>
          <NavLink to="/automation">Automation</NavLink>
          <NavLink to="/accounts">Accounts</NavLink>
          <NavLink to="/csv">CSV leads</NavLink>
        </nav>
      </header>
      <main className="main">
        <Routes>
          <Route path="/" element={<Overview />} />
          <Route path="/leads" element={<Leads />} />
          <Route path="/automation" element={<Automation />} />
          <Route path="/accounts" element={<AccountsPage />} />
          <Route path="/csv" element={<CsvLeads />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </main>
    </div>
  )
}

export default function App() {
  return (
    <HashRouter>
      <Layout />
    </HashRouter>
  )
}
