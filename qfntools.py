import pandas as pd
import numpy as np
from scipy.stats import pearsonr
import statsmodels.api as sm
from scipy.optimize import minimize
import matplotlib.pyplot as plt
from pykalman import KalmanFilter
from statsmodels.sandbox.tools.tools_pca import pca

class EigenPortfolio:
    def __init__(self, req_exp):
        self.req_exp = req_exp
        self.req_pc = None
        self.cov = None
        self.df = None
        self.w = None
        self.v = None
        self.norm_wgt = None
        self.loadings_df = None

    def fit(self, df):
        self.df = df
        self.cov = df.cov()
        w, v = np.linalg.eig(self.cov.to_numpy())
        self.w = w / w.sum()
        self.v = v
        self.req_pc = np.where(self.w.cumsum() > self.req_exp)[0][0] + 1
        self.loadings_df = pd.DataFrame(self.v[:, :self.req_pc], index=df.columns, columns=['PC{}'.format(i + 1) for i in range(self.req_pc)])
        self.norm_wgt = pd.DataFrame(self.v[:, :], index=df.columns, columns=['PC{}'.format(i + 1) for i in range(df.shape[1])])
        self.norm_wgt = self.norm_wgt / self.norm_wgt.sum()

    def price(self, const_rebal=False):
        if const_rebal:
            port_df = pd.DataFrame(np.dot(self.df, self.norm_wgt.iloc[:, :self.req_pc]), index=self.df.index, columns=['PC{}'.format(i+1) for i in range(self.req_pc)])
            port_df = (port_df +1).cumprod()
        else:
            port_df = pd.DataFrame(np.dot((1+self.df).cumprod()-1, self.norm_wgt.iloc[:, :self.req_pc]), index=self.df.index, columns=['PC{}'.format(i+1) for i in range(self.req_pc)])
        port_df = port_df/port_df.iloc[0, :]
        return port_df

    def plot(self, const_rebal = False):
        port_df = self.price(const_rebal)
        port_df.plot()

    def return_(self, const_rebal=False):
        if const_rebal:
            ret_df = pd.DataFrame(np.dot(self.df, self.norm_wgt.iloc[:, :self.req_pc]), index=self.df.index, columns=['PC{}'.format(i+1) for i in range(self.req_pc)])
        else:
            ret_df = pd.DataFrame(np.dot((1+self.df).cumprod()-1, self.norm_wgt.iloc[:, :self.req_pc]), index=self.df.index, columns=['PC{}'.format(i+1) for i in range(self.req_pc)])
            ret_df = ret_df.pct_change()
        return ret_df


class FactorSelection:
    def __init__(self, req_exp, req_corr, max_f_cor):
        self.req_exp = req_exp
        self.req_corr = req_corr
        self.max_f_cor = max_f_cor
        self.eigen_port = None
        self.fac = list()
        self.x = None
        self.R2 = None
        self.betas = None

    def fit(self, y, x):
        self.x = x
        self.eigen_port = EigenPortfolio(self.req_exp)
        self.eigen_port.fit(y)
        fac_id = list()
        fac_p = list()
        eigen_port_df = self.eigen_port.return_(True)

        for p in range(eigen_port_df.shape[1]):
            epi = eigen_port_df.iloc[:, p]
            for f in range(x.shape[1]):
                if f in fac_id:
                    continue
                r, p = pearsonr(x.iloc[:, f], epi)
                if abs(r) >= self.req_corr:
                    fac_id.append(f)
                    fac_p.append(abs(p))

        sort_fac = [x for _, x in sorted(zip(fac_p, fac_id))]

        if len(fac_id) == 0:
            print('All factors < req_corr')
            return

        removed = list()
        for i in range(len(sort_fac)-1):
            if sort_fac[i] in removed:
                continue
            for j in range(i+1, len(sort_fac)):
                if sort_fac[j] in removed:
                    continue
                r, p = pearsonr(x.iloc[:, sort_fac[i]], x.iloc[:, sort_fac[j]])

                if abs(r) > self.max_f_cor:
                    removed.append(sort_fac[j])
        sort_fac = [x for x in sort_fac if x not in removed]
        self.fac = list(x.columns[sort_fac])
        self.build_model()

    def merged_df(self):
        eigen_port_df = self.eigen_port.return_(True)
        merged_df = pd.concat([self.factor_df(), eigen_port_df], axis=1)
        return merged_df

    def factor_df(self):
        return self.x.copy()[self.fac]

    def plot_eigen(self, const_rebal = False):
        self.eigen_port.plot( const_rebal)

    def build_model(self):
        fac_df = self.factor_df()
        fac_df = (fac_df - fac_df.mean()) / fac_df.std()
        eqty_df = self.y
        eqty_df = (eqty_df - eqty_df.mean()) / eqty_df.std()

        X = fac_df.to_numpy()
        X = sm.add_constant(X)
        R2 = list()
        betas = list()
        # Run regression on Equity returns using selected factors as predictors
        for i in range(eqty_df.shape[1]):
            model = sm.OLS(endog=eqty_df.iloc[:, i], exog=X)
            result = model.fit()
            R2.append(result.rsquared)
            betas.append(result.params)

        R2 = pd.DataFrame(R2, index=eqty_df.columns, columns=['R squared'])
        betas = pd.DataFrame(betas, index=eqty_df.columns)
        betas = betas.T
        betas.index = ['intercept'] + list(fac_df.columns)

        self.R2 = R2
        self.betas = betas.T

    def eigen_df(self):
        return self.eigen_port.return_()

    @property
    def y(self):
        return self.eigen_port.df.copy()

class EfficientFrontier:
    def __init__(self, risk_measure, alpha=5):
        self.risk_measure = risk_measure.lower()
        self.df = None

        # percentile for VaR
        self.alpha = alpha

        # covariance matrix
        self.omega = None

        # mean vector
        self.R = None

        self.wgt = None
        self.mu_range = None
        self.risk_range = None

    def fit(self, df, wbnd, mu_range):
        self.R = df.mean()
        self.omega = df.cov()
        self.df = df

        if self.risk_measure == 'cvar':
            obj_func = self.cvar
        elif self.risk_measure == 'var':
            obj_func = self.var
        else:
            obj_func = self.sd

        risk_range = np.zeros(len(mu_range))
        n = df.shape[1]
        wgt = list()

        for i in range(len(mu_range)):
            mu = mu_range[i]

            # initial weight = equal weight
            x_0 = np.ones(n)/n

            # bounds for weightings
            bndsa = [wbnd for j in range(n)]

            # constraint 1 --> type=equality --> sum(weightings) = 1
            # constraint 2 --> type=equality --> np.dot(w^T, R) = mu
            consTR = ({'type':'eq','fun':lambda x:1-np.sum(x)},{'type':'eq','fun':lambda x: mu - np.dot(x, self.R)})

            # Find min risk portfolio for given mu
            w = minimize(obj_func, x_0, method = 'SLSQP', constraints = consTR, bounds = bndsa)

            risk_range[i] = obj_func(w.x)

            wgt.append(np.squeeze(w.x))
        wgt = np.array(wgt)
        self.wgt = wgt
        self.mu_range = mu_range
        self.risk_range = risk_range

    def sd(self, w):
        return np.dot(w, np.dot(self.omega, w.T))

    def cvar(self, w):
        ret = np.dot(self.df, w.T)
        return abs(min((np.mean(ret[ret<=np.percentile(ret, self.alpha)]), 0)))

    def var(self, w):
        ret = np.dot(self.df, w.T)
        return abs(min(np.percentile(ret, self.alpha), 0))

    def weights(self, drop_zero_col=True, rounding=True):
        df = pd.DataFrame(self.wgt, index=[self.mu_range, self.risk_range], columns=self.df.columns)
        df.index.names = ['mu', self.risk_measure ]
        if rounding:
            df = np.round(df, 2)
        if drop_zero_col:
            df = df.loc[:, (df!=0.0).any(axis=0)]
        return df

    def to_csv(self, path):
        self.weights().to_csv(path)

    def plot(self):
        if self.risk_measure == 'cvar':
            label = '{}% Conditional VaR (%)'.format(self.alpha)
        elif self.risk_measure == 'var':
            label = '{}% VaR (%)'.format(self.alpha)
        else:
            label = 'Standard Deviation (%)'
        plt.plot(np.multiply(self.risk_range, 100),np.multiply(self.mu_range, 100),color="red")
        plt.xlabel(label,fontsize=10)
        plt.ylabel("Expected Return (%)",fontsize=10)
        plt.title("Efficient Frontier",fontsize=12)


class DynamicFactorExposure:
    def __init__(self):
        self.kf = None
        self.filter_df = None
        self.smoothed_df = None
        self.filter_state_means = None
        self.filter_state_covs = None
        self.smoothed_state_means = None
        self.smoothed_state_covs = None
        self.x = None
        self.y = None
        self.factor_pca = None
        self.eigen_vec = None

    def fit(self, y, x, factor_pca=False, n_pc=3):

        if factor_pca:
            self.factor_pca = True
            xreduced, factors, evals, evecs = pca(x, keepdim=n_pc)
            self.eigen_vec = pd.DataFrame(evecs, index=x.columns, columns=['PC{}'.format(i) for i in range(1, n_pc+1)])
            x = pd.DataFrame(factors, index=x.index, columns=['PC{}'.format(i) for i in range(1, n_pc+1)])

        self.x = x
        self.y = y
        n_dim_obs = y.shape[1]
        n_dim_state = x.shape[1]+1
        ntimestep = y.shape[0]
        factors = sm.add_constant(x)

        fac_obs = np.array(factors)
        obs_matrics = np.zeros((ntimestep, n_dim_obs, n_dim_state))

        for i in range(n_dim_obs):
            obs_matrics[:, i, :] =fac_obs

        kf = KalmanFilter(n_dim_obs=n_dim_obs, n_dim_state=n_dim_state, transition_matrices=np.eye(factors.shape[1]),
                          observation_matrices=obs_matrics, em_vars=['transition_covariance', 'observation_covariance', 'initial_state_mean', 'initial_state_covariance'] )

        if factor_pca:
            cols = ['Intercept'] + ['beta-PC{}'.format(i) for i in range(1, n_pc+1)]
        else:
            cols=['Intercept'] + ['beta-{}'.format(i) for i in range(1, n_dim_state)]
        self.kf = kf
        self.filter_state_means, self.filter_state_covs = kf.filter(y)

        self.filter_df = pd.DataFrame(self.filter_state_means, index=y.index, columns=cols)
        self.smoothed_state_means, self.smoothed_state_covs = kf.smooth(y)
        self.smoothed_df = pd.DataFrame(self.smoothed_state_means, index=y.index, columns=cols)

    def plot(self, smoothed=False):
        if smoothed:
            self.smoothed_df.plot()
        else:
            self.filter_df.plot()

    def factor_cov(self, smoothed=True):
        if smoothed:
            return self.smoothed_state_covs
        else:
            return self.filter_state_covs