Name:           steading-release
Version:        1.0.0
Release:        1
Summary:        The Steading package repository and its signing key
License:        ISC
URL:            https://pkgs.stead.ing
BuildArch:      noarch
Source0:        steading.repo
Source1:        RPM-GPG-KEY-steading

%description
Configures dnf to install Steading's packages from pkgs.stead.ing, and
installs the key every package and every repository's metadata there is
signed with. The stable channel is enabled; edge is not.

%prep

%build

%install
install -D -p -m 0644 %{SOURCE0} %{buildroot}%{_sysconfdir}/yum.repos.d/steading.repo
install -D -p -m 0644 %{SOURCE1} %{buildroot}%{_sysconfdir}/pki/rpm-gpg/RPM-GPG-KEY-steading

%files
%config(noreplace) %{_sysconfdir}/yum.repos.d/steading.repo
%{_sysconfdir}/pki/rpm-gpg/RPM-GPG-KEY-steading
