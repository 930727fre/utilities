import { MantineProvider, Box } from '@mantine/core';
import { HashRouter, Routes, Route, Navigate, useLocation } from 'react-router-dom';
import { Notifications } from '@mantine/notifications';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

import '@mantine/core/styles.css';
import '@mantine/notifications/styles.css';

import DashboardPage from './pages/DashboardPage';
import PracticePage from './pages/PracticePage';
import DrillPage from './pages/DrillPage';

const queryClient = new QueryClient();

function AnimatedRoutes() {
  const location = useLocation();
  return (
    <div key={location.key} className="page-enter">
      <Routes>
        <Route path="/" element={<DashboardPage />} />
        <Route path="/practice" element={<PracticePage />} />
        <Route path="/drill" element={<DrillPage />} />
        <Route path="*" element={<Navigate to="/" />} />
      </Routes>
    </div>
  );
}

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <MantineProvider defaultColorScheme="dark">
        <Notifications />
        <Box
          style={{
            backgroundColor: '#1c1c1e',
            height: '100%',
            color: '#e8e3d9',
            display: 'flex',
            flexDirection: 'column',
          }}
        >
          <HashRouter>
            <AnimatedRoutes />
          </HashRouter>
        </Box>
      </MantineProvider>
    </QueryClientProvider>
  );
}
