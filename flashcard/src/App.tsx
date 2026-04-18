import { MantineProvider, Box } from '@mantine/core';
import { HashRouter, Routes, Route, Navigate } from 'react-router-dom';
import { Notifications } from '@mantine/notifications';

import '@mantine/core/styles.css';
import '@mantine/notifications/styles.css';

import DashboardPage from './pages/DashboardPage';
import ReviewPage from './pages/ReviewPage';
import BatchAddPage from './pages/BatchAddPage';
import EditPage from './pages/EditPage';

function App() {
  return (
    <MantineProvider defaultColorScheme="dark">
      <Notifications />
      <Box
        style={{
          backgroundColor: '#0a0c14',
          minHeight: '100vh',
          color: '#e8eaf0'
        }}
      >
        <HashRouter>
          <Routes>
            <Route path="/" element={<DashboardPage />} />
            <Route path="/review" element={<ReviewPage />} />
            <Route path="/batch-add" element={<BatchAddPage />} />
            <Route path="/edit" element={<EditPage />} />
            <Route path="*" element={<Navigate to="/" />} />
          </Routes>
        </HashRouter>
      </Box>
    </MantineProvider>
  );
}

export default App;
