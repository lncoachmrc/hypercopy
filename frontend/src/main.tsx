import React from 'react';
import ReactDOM from 'react-dom/client';
import {BrowserRouter} from 'react-router-dom';
import App from './App';
import {AuthProvider} from './auth';
import {initLanguage} from './i18n';
import {initAutoTranslate} from './autoTranslate';
import {translateDialogText} from './dialogTranslations';
import './styles.css';
import './i18n.css';

initLanguage();

const nativeConfirm=window.confirm.bind(window);
window.confirm=(message?:string)=>nativeConfirm(translateDialogText(String(message??'')));

ReactDOM.createRoot(document.getElementById('root')!).render(<React.StrictMode><BrowserRouter><AuthProvider><App/></AuthProvider></BrowserRouter></React.StrictMode>);

initAutoTranslate();
