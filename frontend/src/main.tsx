import React from 'react'
import ReactDOM from 'react-dom/client'
import { createBrowserRouter, RouterProvider } from 'react-router-dom'
import { Layout } from './components/Layout'
import { Clones } from './routes/Clones'
import { Snapshots } from './routes/Snapshots'
import { Config } from './routes/Config'
import { CloneDetail } from './routes/CloneDetail'
import { ReplicationTables } from './routes/ReplicationTables'
import './styles.css'

const router = createBrowserRouter([
    {
        element: <Layout />,
        children: [
            { path: '/', element: <Clones /> },
            { path: '/snapshots', element: <Snapshots /> },
            { path: '/config', element: <Config /> },
            { path: '/clones/:cloneId', element: <CloneDetail /> },
            { path: '/replication', element: <ReplicationTables /> },
        ],
    },
])

ReactDOM.createRoot(document.getElementById('root')!).render(
    <React.StrictMode>
        <RouterProvider router={router} />
    </React.StrictMode>,
)
