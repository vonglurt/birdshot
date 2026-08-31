# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul Richeson
# FreeBSD port for the native line. The tree is plain C++17 + CMake with no
# dependencies, so the port is exactly this small.
PORTNAME=	birdshot
DISTVERSION=	2.0.0-rc1
CATEGORIES=	graphics
MASTER_SITES=	https://github.com/vonglurt/birdshot/releases/download/v${DISTVERSION}/

MAINTAINER=	ports@birdshot.org
COMMENT=	Bird and sky capture with solar planning, native line
WWW=		https://birdshot.org

LICENSE=	MIT
LICENSE_FILE=	${WRKSRC}/LICENSE

USES=		cmake
CMAKE_SOURCE_PATH=	${WRKSRC}/native

do-test:
	${WRKDIR}/.build/birdshot selftest

PLIST_FILES=	bin/birdshot

.include <bsd.port.mk>
