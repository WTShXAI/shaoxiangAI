import { createBrowserRouter, Navigate } from 'react-router-dom'
import AppLayout from '@/components/layout/AppLayout'
import PredictionHall from '@/pages/PredictionHall'
import MatchAnalysis from '@/pages/MatchAnalysis'
import ModelManagement from '@/pages/ModelManagement'
import SystemMonitor from '@/pages/SystemMonitor'
import DataExplorer from '@/pages/DataExplorer'
const router = createBrowserRouter([
  {
    path: '/',
    element: <AppLayout />,
    children: [
      { index: true, element: <PredictionHall /> },
      { path: 'match-analysis', element: <MatchAnalysis /> },
      { path: 'model-management', element: <ModelManagement /> },
      { path: 'system-monitor', element: <SystemMonitor /> },
      { path: 'data-explorer', element: <DataExplorer /> },
      { path: '*', element: <Navigate to="/" replace /> },
    ],
  },
])
export default router