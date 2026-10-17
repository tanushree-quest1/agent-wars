import { Injectable } from '@angular/core';

@Injectable({ providedIn: 'root' })
export class ApiService {
  async get(path: string): Promise<any> {
    return (await fetch(path)).json();
  }

  async post(path: string, body?: any): Promise<any> {
    return (await fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body ?? {}),
    })).json();
  }

  async postForm(path: string, form: FormData): Promise<any> {
    return (await fetch(path, { method: 'POST', body: form })).json();
  }
}
