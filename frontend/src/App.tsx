/** Routing. Role decides which surface a signed-in account sees. */

import { Navigate, Route, Routes } from 'react-router-dom'
import { useAuth } from './lib/auth'
import { Shell } from './components/Shell'
import { Spinner } from './components/ui'
import { Login } from './pages/Login'
import { DriverHome } from './pages/DriverHome'
import { FindParking } from './pages/FindParking'
import { WalletPage } from './pages/WalletPage'
import { History } from './pages/History'
import { OwnerDashboard } from './pages/OwnerDashboard'
import { LayoutBuilder } from './pages/LayoutBuilder'
import { GateConsole } from './pages/GateConsole'
import { Analytics } from './pages/Analytics'
import { Operations } from './pages/Operations'
import { Automation } from './pages/Automation'
import { EvaluationLab } from './pages/EvaluationLab'

export function App() {
  const { user, loading, isOwner } = useAuth()

  if (loading) {
    return (
      <div className="flex h-full items-center justify-center">
        <Spinner label="Starting SmartPark…" />
      </div>
    )
  }

  if (!user) return <Login />

  return (
    <Shell>
      <Routes>
        {isOwner ? (
          <>
            <Route path="/" element={<OwnerDashboard />} />
            <Route path="/layout" element={<LayoutBuilder />} />
            <Route path="/gates" element={<GateConsole />} />
            <Route path="/analytics" element={<Analytics />} />
            <Route path="/operations" element={<Operations />} />
            <Route path="/automation" element={<Automation />} />
            <Route path="/lab" element={<EvaluationLab />} />
          </>
        ) : (
          <>
            <Route path="/" element={<DriverHome />} />
            <Route path="/find" element={<FindParking />} />
            <Route path="/wallet" element={<WalletPage />} />
            <Route path="/history" element={<History />} />
          </>
        )}
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </Shell>
  )
}
