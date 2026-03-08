import React from 'react'
import ReactDOM from 'react-dom/client'
import { createBrowserRouter, RouterProvider } from 'react-router-dom'
import { App } from './routes/App'
import { CloneDetail } from './routes/CloneDetail'
import { ReplicationTables } from './routes/ReplicationTables'
import './styles.css'

const router = createBrowserRouter([
    { path: '/', element: <App /> },
    { path: '/clones/:cloneId', element: <CloneDetail /> },
    { path: '/replication', element: <ReplicationTables /> },
])

ReactDOM.createRoot(document.getElementById('root')!).render(
    <React.StrictMode>
        <RouterProvider router={router} />
    </React.StrictMode>,
)
