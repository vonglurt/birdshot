# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Paul Richeson
#
# FreeBSD ports skeleton (docs/PACKAGING.md, 4.6). Untested -- it exists so
# the shape is settled and a BSD user has somewhere to start; expect to touch
# the Qt and camera dependencies. Capture has no BSD backend yet; the CLI
# tools (sessions, assemble, exif, doctor) are the useful surface.

PORTNAME=	birdshot
DISTVERSIONPREFIX=	v
DISTVERSION=	1.1.0-rc1
CATEGORIES=	graphics python

MAINTAINER=	ports@birdshot.org
COMMENT=	Bird and sky capture for the Raspberry Pi HQ Camera
WWW=		https://birdshot.org

LICENSE=	MIT
LICENSE_FILE=	${WRKSRC}/LICENSE

RUN_DEPENDS=	${PYNUMPY} \
		ffmpeg:multimedia/ffmpeg

USES=		python
USE_PYTHON=	pep517 autoplist
USE_GITHUB=	yes
GH_ACCOUNT=	vonglurt

NO_ARCH=	yes

.include <bsd.port.mk>
