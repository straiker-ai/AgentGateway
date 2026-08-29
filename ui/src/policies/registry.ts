import {
	Braces,
	CircuitBoard,
	FileKey2,
	Fingerprint,
	KeyRound,
	LockKeyhole,
	Shield,
	ShieldCheck,
	SlidersHorizontal,
	Timer,
	Workflow
} from 'lucide-react';
import { createElement, type ComponentType } from 'react';

import straikerIcon from '@/assets/providers/straiker.svg';
import type { PolicyKey } from '@/policies/types';

// The Straiker mark, used wherever a Straiker guard/policy is listed so the brand is consistent
// across every path (the LLM guardrail, this coding route policy, MCP).
const StraikerMark: ComponentType<{ size?: number }> = ({ size = 16 }) =>
	createElement('img', { src: straikerIcon, width: size, height: size, alt: '' });

export const policyUi: Partial<
	Record<
		PolicyKey,
		{
			title: string;
			icon: ComponentType<{ size?: number }>;
			customEditor?:
				| 'authorization'
				| 'backendAuth'
				| 'cors'
				| 'extAuthz'
				| 'extProc'
				| 'jwtAuth'
				| 'localRateLimit'
				| 'mcpAuthentication'
				| 'mcpAuthorization'
				| 'mcpGuardrails'
				| 'oidc'
				| 'remoteRateLimit'
				| 'transformations';
		}
	>
> = {
	apiKey: { title: 'API keys', icon: KeyRound },
	authorization: {
		title: 'Authorization',
		icon: ShieldCheck,
		customEditor: 'authorization'
	},
	backendAuth: {
		title: 'Backend auth',
		icon: LockKeyhole,
		customEditor: 'backendAuth'
	},
	basicAuth: { title: 'Basic auth', icon: LockKeyhole },
	cors: { title: 'CORS', icon: Workflow, customEditor: 'cors' },
	extAuthz: {
		title: 'External authz',
		icon: CircuitBoard,
		customEditor: 'extAuthz'
	},
	extProc: {
		title: 'External processor',
		icon: SlidersHorizontal,
		customEditor: 'extProc'
	},
	straikerCoding: { title: 'Straiker (coding)', icon: StraikerMark },
	jwtAuth: { title: 'JWT auth', icon: FileKey2, customEditor: 'jwtAuth' },
	localRateLimit: {
		title: 'Local rate limit',
		icon: Timer,
		customEditor: 'localRateLimit'
	},
	mcpAuthentication: {
		title: 'MCP authentication',
		icon: KeyRound,
		customEditor: 'mcpAuthentication'
	},
	mcpAuthorization: {
		title: 'MCP authorization',
		icon: ShieldCheck,
		customEditor: 'mcpAuthorization'
	},
	mcpGuardrails: {
		title: 'MCP guardrails',
		icon: Shield,
		customEditor: 'mcpGuardrails'
	},
	oidc: { title: 'OIDC', icon: Fingerprint, customEditor: 'oidc' },
	remoteRateLimit: {
		title: 'Remote rate limit',
		icon: Braces,
		customEditor: 'remoteRateLimit'
	},
	transformations: {
		title: 'Transformations',
		icon: Shield,
		customEditor: 'transformations'
	}
};
